"""The bench-endpoint session: stand up the bench endpoint for one run and always tear it down.

The bench endpoint (``terraform/bench-endpoint``) lives for a flow run: the session applies
the stack, reads the bench host's instance id, waits for the host's boot sentinel, yields
the bench host, and destroys the stack on the way out.
"""

import json
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The terraform transport: run ``terraform <args>`` with extra environment, return stdout,
# and raise on a non-zero exit. Stateless and single-op, so a typed Callable, not a Protocol.
Terraform = Callable[[Sequence[str], Mapping[str, str]], str]


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
    s3: Any,
    stack_dir: Path,
    topology: BenchEndpointTopology,
    api_key: str,
    results_bucket: str,
    boot_timeout_s: float = 420.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[BenchHost]:
    """Apply the bench-endpoint stack, yield its bench host, and destroy the stack on exit."""
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
                [chdir, "output", "-raw", "bench_host_instance_id"], env
            ).strip()
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
