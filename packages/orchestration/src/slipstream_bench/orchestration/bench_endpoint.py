"""The bench-endpoint session: stand up the bench endpoint for one run and always tear it down.

The bench endpoint (``terraform/bench-endpoint``) lives for a flow run: the session applies
the stack, reads the bench host's instance id, waits for the host's boot sentinel, yields
the bench host, and destroys the stack on the way out. Once the apply starts, the destroy
runs whatever happens next — a failed apply, a host that never boots, a raising body — so a
run never leaves the endpoint billing.

The module imports no Prefect: the knob sweep's flow opens the session around its core, and
its tests drive it against a fake :data:`Terraform` and a fake S3 client. Terraform runs in a
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

# The terraform transport: run ``terraform <args>`` with extra environment, return stdout,
# and raise on a non-zero exit. Stateless and single-op, so a typed Callable, not a Protocol.
Terraform = Callable[[Sequence[str], Mapping[str, str]], str]


class TerraformError(Exception):
    """A ``terraform`` invocation that exited non-zero."""


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


# The host writes boot-status/<instance-id> to the results bucket when cloud-init ends:
# "ok", or "failed: <step>". Until then the key is absent, which S3 reports as NoSuchKey.
_BOOT_STATUS_PREFIX = "boot-status"
_BOOT_OK = "ok"
_BOOT_FAILED_PREFIX = "failed:"
_NOT_WRITTEN_YET = "NoSuchKey"

_INSTANCE_ID_OUTPUT = "bench_host_instance_id"


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


class TopologyError(ValueError):
    """The worker environment lacks, or malforms, a bench-endpoint topology variable."""


def read_topology(environ: Mapping[str, str]) -> BenchEndpointTopology:
    """Read the bench-endpoint topology from the worker's environment.

    The eks stack's ``bench-endpoint-topology`` ConfigMap sets these on the worker: plain
    strings, and the two lists as JSON arrays.

    :param environ: the environment to read (the worker's ``os.environ``).
    :return: the topology the session applies the stack against.
    :raise TopologyError: naming the variable that is unset, empty, or not a non-empty
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


def _read_string(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise TopologyError(
            f"{name} is unset or empty; the worker reads it from the eks stack's "
            f"bench-endpoint-topology ConfigMap"
        )
    return value


def _read_strings(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = _read_string(environ, name)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise TopologyError(f"{name} is not JSON: {raw!r}") from error
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise TopologyError(
            f"{name} must be a non-empty JSON array of strings: {raw!r}"
        )
    return tuple(value)


@dataclass(frozen=True)
class BenchHost:
    """The bench host the session stood up, addressed by its EC2 instance id."""

    instance_id: str


@contextmanager
def open_bench_endpoint(
    *,
    terraform: Terraform,
    s3: Any,
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

    init runs before the destroy is armed: it changes no infrastructure, and a destroy
    without an initialised backend would fail too. Every later step is followed by the
    destroy. A failed destroy is the error raised, since it leaves resources billing; a
    body or apply failure before it stays on it as ``__context__``.

    :param terraform: the terraform transport (see :func:`build_terraform`).
    :param s3: a boto3 S3 client (or a stand-in exposing ``get_object``), read for the
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
    :raise BenchHostBootError: when the apply yields no instance id, or the host reports a
        failed boot or none within ``boot_timeout_s``.
    :raise ClientError: when the sentinel read fails for a reason other than its absence.
    """
    with tempfile.TemporaryDirectory() as workdir:
        shutil.copytree(stack_dir, workdir, dirs_exist_ok=True)
        chdir = f"-chdir={workdir}"
        env = _get_stack_variables(topology, api_key=api_key)
        terraform(
            [
                chdir,
                "init",
                "-input=false",
                "-lockfile=readonly",
                f"-backend-config=bucket={topology.state_bucket}",
            ],
            env,
        )
        try:
            terraform([chdir, "apply", "-auto-approve", "-input=false"], env)
            instance_id = terraform(
                [chdir, "output", "-raw", _INSTANCE_ID_OUTPUT], env
            ).strip()
            if not instance_id:
                raise BenchHostBootError(
                    f"terraform output {_INSTANCE_ID_OUTPUT} is empty after apply"
                )
            _wait_for_boot(
                s3,
                bucket=results_bucket,
                instance_id=instance_id,
                timeout_s=boot_timeout_s,
                poll_interval_s=poll_interval_s,
                sleep=sleep,
                now=now,
            )
            yield BenchHost(instance_id=instance_id)
        finally:
            terraform([chdir, "destroy", "-auto-approve", "-input=false"], env)


def _get_stack_variables(
    topology: BenchEndpointTopology, *, api_key: str
) -> dict[str, str]:
    """Map the topology and api-key onto the stack's variables as ``TF_VAR_*`` environment.

    Every variable travels in the environment rather than as ``-var`` so the api-key never
    lands in the terraform argv, which any process on the node can read. Terraform parses
    a list variable's environment value as HCL, which a JSON list is.
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


def _wait_for_boot(
    s3: Any,
    *,
    bucket: str,
    instance_id: str,
    timeout_s: float,
    poll_interval_s: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> None:
    """Poll the host's boot sentinel until it reports ``ok``.

    :raise BenchHostBootError: on a ``failed: <step>`` sentinel, or when none reports ready
        within ``timeout_s``.
    :raise ClientError: on any S3 read failure other than the sentinel not written yet.
    """
    key = f"{_BOOT_STATUS_PREFIX}/{instance_id}"
    deadline = now() + timeout_s
    while True:
        status = _read_sentinel(s3, bucket=bucket, key=key)
        if status == _BOOT_OK:
            return
        if status is not None and status.startswith(_BOOT_FAILED_PREFIX):
            raise BenchHostBootError(f"bench host {instance_id} boot {status}")
        if now() >= deadline:
            raise BenchHostBootError(
                f"bench host {instance_id} did not report ready within "
                f"{timeout_s:.0f}s (last: {status or '<no sentinel yet>'!r})"
            )
        sleep(poll_interval_s)


def _read_sentinel(s3: Any, *, bucket: str, key: str) -> str | None:
    """Return the sentinel's text, or None while the host has not written it yet."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == _NOT_WRITTEN_YET:
            return None
        raise
    return response["Body"].read().decode().strip()
