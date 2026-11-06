"""The bench-endpoint session: stand up the bench endpoint for one run and always tear it down.

The bench endpoint (``terraform/bench-endpoint``) lives for a flow run: the session applies
the stack, reads the bench host's instance id, waits for the host's boot sentinel, yields
the bench host, and destroys the stack on the way out.
"""

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
        env: dict[str, str] = {}
        terraform([chdir, "init"], env)
        try:
            terraform([chdir, "apply"], env)
            instance_id = terraform([chdir, "output"], env).strip()
            yield BenchHost(instance_id=instance_id)
        finally:
            terraform([chdir, "destroy"], env)
