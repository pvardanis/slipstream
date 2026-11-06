"""The flows' crash and cancellation hook: destroy the run's bench endpoint once it is gone.

Prefect runs a cancelled run's hooks in the worker after killing the run's process, and a
crashed run's either there or in the dying process, so the hook rebuilds everything the
teardown needs from the run's parameters and the worker's environment. These tests call the
hook as Prefect does, with terraform, kubectl, and S3 swapped for recording fakes, and check
both flows register it.
"""

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prefect.client.schemas.objects import FlowRun
from prefect.states import Cancelling, Crashed

import slipstream_bench.orchestration.flows.run_setup as run_setup_module
from slipstream_bench.orchestration.flows.knob_sweep import knob_sweep_flow
from slipstream_bench.orchestration.flows.kv_ab import kv_ab_flow
from slipstream_bench.orchestration.flows.run_setup import destroy_run_bench_endpoint
from tests.orchestration.bench_endpoint_fakes import API_KEY, WORKER_ENV


class _RecordingTerraform:
    """Record each terraform argv and environment, answering every command with nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> str:
        self.calls.append((list(args), dict(env)))
        return ""

    def subcommands(self) -> list[str]:
        return [
            next(a for a in args if not a.startswith("-")) for args, _ in self.calls
        ]


def _serve_api_key(args: Sequence[str], _stdin: str | None) -> str:
    assert "secret" in args
    return base64.b64encode(API_KEY.encode("utf-8")).decode("ascii")


@pytest.fixture
def terraform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RecordingTerraform:
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "main.tf").write_text("# stack\n", encoding="utf-8")
    for name, value in WORKER_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BENCH_ENDPOINT_STACK_DIR", str(stack))
    recorder = _RecordingTerraform()
    monkeypatch.setattr(run_setup_module, "build_terraform", lambda: recorder)
    monkeypatch.setattr(run_setup_module, "build_kubectl", lambda: _serve_api_key)
    return recorder


def _flow_run() -> FlowRun:
    return FlowRun(
        flow_id=uuid4(),
        parameters={"run_id": "run-7", "region": "eu-west-1", "bucket": "bench"},
    )


@pytest.mark.parametrize("state", [Cancelling(), Crashed()])
def test_hook_destroys_the_endpoint_from_the_workers_environment(
    terraform: _RecordingTerraform, monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    regions: list[str] = []
    monkeypatch.setattr(
        run_setup_module, "build_s3_client", lambda region: regions.append(region)
    )

    destroy_run_bench_endpoint(flow=knob_sweep_flow, flow_run=_flow_run(), state=state)

    assert terraform.subcommands() == ["init", "destroy"]
    _args, env = terraform.calls[-1]
    assert env["TF_VAR_vllm_api_key"] == API_KEY
    assert env["TF_VAR_state_bucket"] == WORKER_ENV["BENCH_ENDPOINT_STATE_BUCKET"]
    # The errored-state copy goes through a client in the run's region.
    assert regions == ["eu-west-1"]


@pytest.mark.parametrize("parent_flow", [knob_sweep_flow, kv_ab_flow])
def test_both_flows_destroy_their_endpoint_on_a_crash_or_cancellation(
    parent_flow: Any,
) -> None:
    assert parent_flow.on_cancellation_hooks == [destroy_run_bench_endpoint]
    assert parent_flow.on_crashed_hooks == [destroy_run_bench_endpoint]
