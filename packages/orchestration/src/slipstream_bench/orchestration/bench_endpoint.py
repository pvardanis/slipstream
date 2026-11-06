"""The bench-endpoint session: stand up the bench endpoint for one run and always tear it down.

The bench endpoint (``terraform/bench-endpoint``) lives for a flow run: the session applies
the stack, reads the bench host's instance id, waits for the host's boot sentinel, yields
the bench host, and destroys the stack on the way out. Once the apply starts, the destroy
runs whatever happens next — a failed apply, a host that never boots, a raising body — so a
run never leaves the endpoint billing.

The module imports no Prefect, so a flow can open the session around its work and its tests
drive it against a fake :data:`Terraform` and a fake S3 client. Terraform runs in a
writable copy of the stack, because the orchestration image bakes the stack on a read-only
root where terraform cannot write ``.terraform``.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

# The host writes boot-status/<instance-id> to the results bucket when cloud-init ends:
# "ok", or "failed: <step>; see <log>". Until then the key is absent, which S3 reports as
# NoSuchKey.
_BOOT_STATUS_PREFIX = "boot-status"
_BOOT_OK = "ok"
_BOOT_FAILED_PREFIX = "failed:"
_NOT_WRITTEN_YET = "NoSuchKey"

_INSTANCE_ID_OUTPUT = "bench_host_instance_id"

# The stack's state key, fixed in terraform/bench-endpoint/backend.tf; named in errors so an
# operator can find the state a run left behind.
_STATE_KEY = "bench-endpoint/terraform.tfstate"

# Where the worker's environment variables come from, named when one is missing.
_TOPOLOGY_SOURCE = "the eks stack's bench-endpoint-topology ConfigMap"
_STACK_DIR_SOURCE = (
    "the orchestration image, which bakes the stack there "
    "(a workstation run points it at a checkout of terraform/bench-endpoint)"
)

# The terraform transport: run ``terraform <args>`` with extra environment, return stdout,
# and raise on a non-zero exit. Stateless and single-op, so a typed Callable, not a Protocol.
Terraform = Callable[[Sequence[str], Mapping[str, str]], str]


class TerraformError(Exception):
    """A ``terraform`` invocation that exited non-zero."""


class BenchEndpointEnvironmentError(ValueError):
    """The worker environment lacks, or malforms, a ``BENCH_ENDPOINT_*`` variable.

    Covers every variable the session is applied from: the topology and the baked stack's path.
    """


class BenchEndpointInUseError(Exception):
    """The bench-endpoint state already holds an endpoint this session did not create."""


class BenchHostBootError(Exception):
    """The bench host failed to boot, never reported ready, or was not created."""


@dataclass(frozen=True)
class BenchEndpointTopology:
    """The cluster network and state location the bench-endpoint stack applies against."""

    state_bucket: str
    vpc_id: str
    public_subnets: tuple[str, ...]
    node_security_group_id: str
    node_autoscaling_groups: tuple[str, ...]
    instance_profile: str


@dataclass(frozen=True)
class BenchHost:
    """The bench host the session stood up, addressed by its EC2 instance id."""

    instance_id: str


@contextmanager
def open_bench_endpoint(
    *,
    terraform: Terraform,
    s3_client: Any,
    stack_dir: Path,
    topology: BenchEndpointTopology,
    api_key: str,
    results_bucket: str,
    boot_timeout_s: float = 420.0,
    poll_interval_s: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Iterator[BenchHost]:
    """Apply the bench-endpoint stack, yield its bench host, and destroy the stack on exit.

    The destroy is armed only once the session knows the state is its own. init changes
    no infrastructure, and a destroy without an initialised backend would fail too. The
    state key is shared with ``just bench-endpoint-up`` and every other run, so a state
    that already holds resources belongs to another endpoint: applying would adopt it and
    the exit would destroy it under its owner, so the session raises instead. Every later
    step is followed by the destroy. A failed destroy is the error raised, noted with the
    state left behind, since it leaves resources billing; any earlier failure stays on it
    as ``__context__``.

    :param terraform: the terraform transport (see :func:`build_terraform`).
    :param s3_client: a boto3 S3 client (or a stand-in exposing ``get_object``), read for the
        host's boot sentinel.
    :param stack_dir: the bench-endpoint stack to apply, copied before terraform runs.
    :param topology: the cluster network and state bucket the stack applies against.
    :param api_key: the vLLM api-key the stack publishes for the bench host.
    :param results_bucket: the bench-results bucket the host writes its sentinel to.
    :param boot_timeout_s: the ceiling on waiting for the host's boot sentinel.
    :param poll_interval_s: the wait between sentinel reads.
    :param sleep: the wait function, injected for tests.
    :param now: the monotonic clock, injected for tests.
    :return: a context manager yielding the booted :class:`BenchHost`.
    :raise TerraformError: when a terraform command exits non-zero (from the production
        transport).
    :raise BenchEndpointInUseError: when the state already holds resources.
    :raise BenchHostBootError: when the apply yields no instance id, or the host reports a
        failed or unrecognised boot status, or none within ``boot_timeout_s``.
    :raise ClientError: when the sentinel read fails for a reason other than its absence.
    """
    # The state is remote, so a temporary copy that fails to clean up loses nothing, and
    # its cleanup error must not replace a destroy failure.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        shutil.copytree(stack_dir, workdir, dirs_exist_ok=True)
        chdir = f"-chdir={workdir}"
        env = _get_stack_env_vars(topology, api_key=api_key)
        terraform(
            [
                chdir,
                "init",
                "-input=false",
                "-no-color",
                "-lockfile=readonly",
                f"-backend-config=bucket={topology.state_bucket}",
            ],
            env,
        )
        _refuse_held_state(terraform, chdir=chdir, env=env, topology=topology)
        try:
            terraform(
                [chdir, "apply", "-auto-approve", "-input=false", "-no-color"], env
            )
            instance_id = terraform(
                [chdir, "output", "-no-color", "-raw", _INSTANCE_ID_OUTPUT], env
            ).strip()
            if not instance_id:
                raise BenchHostBootError(
                    f"terraform output {_INSTANCE_ID_OUTPUT} is empty after apply"
                )
            _wait_for_boot(
                s3_client,
                bucket=results_bucket,
                instance_id=instance_id,
                timeout_s=boot_timeout_s,
                poll_interval_s=poll_interval_s,
                sleep=sleep,
                now=now,
            )
            yield BenchHost(instance_id=instance_id)
        finally:
            _destroy(terraform, chdir=chdir, env=env, topology=topology)


def read_topology(environ: Mapping[str, str]) -> BenchEndpointTopology:
    """Read the bench-endpoint topology from the worker's environment.

    The eks stack's ``bench-endpoint-topology`` ConfigMap sets these on the worker: plain
    strings, and the two lists as JSON arrays.

    :param environ: the environment to read (the worker's ``os.environ``).
    :return: the topology the session applies the stack against.
    :raise BenchEndpointEnvironmentError: naming the variable that is unset, empty, or not a non-empty
        JSON array of strings.
    """
    return BenchEndpointTopology(
        state_bucket=_read_string(environ, "BENCH_ENDPOINT_STATE_BUCKET"),
        vpc_id=_read_string(environ, "BENCH_ENDPOINT_VPC_ID"),
        public_subnets=_read_strings(environ, "BENCH_ENDPOINT_PUBLIC_SUBNETS"),
        node_security_group_id=_read_string(
            environ, "BENCH_ENDPOINT_NODE_SECURITY_GROUP_ID"
        ),
        node_autoscaling_groups=_read_strings(
            environ, "BENCH_ENDPOINT_NODE_AUTOSCALING_GROUPS"
        ),
        instance_profile=_read_string(environ, "BENCH_ENDPOINT_INSTANCE_PROFILE"),
    )


def read_stack_dir(environ: Mapping[str, str]) -> Path:
    """Read the path of the bench-endpoint stack the orchestration image bakes.

    :param environ: the environment to read (the worker's ``os.environ``).
    :return: the stack directory the session copies and applies.
    :raise BenchEndpointEnvironmentError: when ``BENCH_ENDPOINT_STACK_DIR`` is unset or empty.
    """
    return Path(
        _read_string(environ, "BENCH_ENDPOINT_STACK_DIR", source=_STACK_DIR_SOURCE)
    )


def build_terraform(*, binary: str = "terraform") -> Terraform:
    """Build the production :data:`Terraform`: shell out to the ``terraform`` binary.

    The extra environment is merged over the worker's own, so the worker's AWS credentials
    and the image's ``TF_CLI_CONFIG_FILE`` provider mirror reach terraform beside the stack
    variables. A non-zero exit raises :class:`TerraformError` carrying the argv, exit code,
    and stderr — never the environment, which holds the api-key.

    :param binary: the terraform executable name (overridable for tests).
    :return: the callable the session sends terraform commands through.
    """

    def run(args: Sequence[str], env: Mapping[str, str]) -> str:
        completed = subprocess.run(
            [binary, *args],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise TerraformError(
                f"terraform {' '.join(args)} exited {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        return completed.stdout

    return run


def _get_stack_env_vars(
    topology: BenchEndpointTopology, *, api_key: str
) -> dict[str, str]:
    """Map the topology and api-key onto the stack's variables as ``TF_VAR_*`` environment.

    Every variable travels in the environment rather than as ``-var`` so the api-key never
    lands in the terraform argv, which any process that can see the terraform process can
    read. Terraform parses a list variable's environment value as HCL, which a JSON list is.
    """
    return {
        "TF_VAR_state_bucket": topology.state_bucket,
        "TF_VAR_vpc_id": topology.vpc_id,
        "TF_VAR_public_subnets": json.dumps(list(topology.public_subnets)),
        "TF_VAR_node_security_group_id": topology.node_security_group_id,
        "TF_VAR_node_autoscaling_groups": json.dumps(
            list(topology.node_autoscaling_groups)
        ),
        "TF_VAR_bench_host_instance_profile": topology.instance_profile,
        "TF_VAR_vllm_api_key": api_key,
    }


def _refuse_held_state(
    terraform: Terraform,
    *,
    chdir: str,
    env: Mapping[str, str],
    topology: BenchEndpointTopology,
) -> None:
    """Raise when the backend's state already holds resources.

    ``show -json`` on an empty state carries no ``values``; once anything is applied it
    lists the resources under ``values.root_module``. Its stdout carries state values,
    sensitive ones included, so it is parsed here and never logged.

    :raise BenchEndpointInUseError: naming the state when it holds any resource.
    """
    state = json.loads(terraform([chdir, "show", "-json", "-no-color"], env))
    root_module = state.get("values", {}).get("root_module", {})
    if root_module.get("resources") or root_module.get("child_modules"):
        raise BenchEndpointInUseError(
            f"{_get_state_url(topology)} already holds a bench endpoint this run did not "
            f"create; destroy it (`just bench-endpoint-down`) or wait for its run to end"
        )


def _wait_for_boot(
    s3_client: Any,
    *,
    bucket: str,
    instance_id: str,
    timeout_s: float,
    poll_interval_s: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> None:
    """Poll the host's boot sentinel until it reports ``ok``.

    :raise BenchHostBootError: on a ``failed: <step>`` sentinel or any other text but
        ``ok`` (the host writes it in one put, so it will not change), or when none reports
        ready within ``timeout_s``.
    :raise ClientError: on any S3 read failure other than the sentinel not written yet.
    """
    key = f"{_BOOT_STATUS_PREFIX}/{instance_id}"
    deadline = now() + timeout_s
    while True:
        status = _read_sentinel(s3_client, bucket=bucket, key=key)
        if status == _BOOT_OK:
            return
        if status is not None and status.startswith(_BOOT_FAILED_PREFIX):
            raise BenchHostBootError(f"bench host {instance_id} boot {status}")
        if status is not None:
            raise BenchHostBootError(
                f"bench host {instance_id} wrote an unrecognised boot status {status!r}"
            )
        if now() >= deadline:
            raise BenchHostBootError(
                f"bench host {instance_id} did not report ready within "
                f"{timeout_s:.0f}s (no sentinel at s3://{bucket}/{key})"
            )
        sleep(poll_interval_s)


def _read_sentinel(s3_client: Any, *, bucket: str, key: str) -> str | None:
    """Return the sentinel's text, or None while the host has not written it yet."""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == _NOT_WRITTEN_YET:
            return None
        raise
    return response["Body"].read().decode().strip()


def _destroy(
    terraform: Terraform,
    *,
    chdir: str,
    env: Mapping[str, str],
    topology: BenchEndpointTopology,
) -> None:
    """Destroy the stack, noting on a failure where the state left behind lives."""
    try:
        terraform([chdir, "destroy", "-auto-approve", "-input=false", "-no-color"], env)
    except Exception as error:
        error.add_note(
            f"bench-endpoint resources may still be billing; their state is "
            f"{_get_state_url(topology)} (`just bench-endpoint-down` destroys from it)"
        )
        raise


def _get_state_url(topology: BenchEndpointTopology) -> str:
    return f"s3://{topology.state_bucket}/{_STATE_KEY}"


def _read_string(
    environ: Mapping[str, str], name: str, *, source: str = _TOPOLOGY_SOURCE
) -> str:
    value = environ.get(name, "")
    if not value:
        raise BenchEndpointEnvironmentError(
            f"{name} is unset or empty; the worker reads it from {source}"
        )
    return value


def _read_strings(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = _read_string(environ, name)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BenchEndpointEnvironmentError(f"{name} is not JSON: {raw!r}") from error
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise BenchEndpointEnvironmentError(
            f"{name} must be a non-empty JSON array of strings: {raw!r}"
        )
    return tuple(value)
