"""The flows' crash and cancellation hook that releases the run's rollout freeze.

A killed run's freeze session leaves the release to this hook, which Prefect runs after
the run's process has ended. These tests call the hook as Prefect does, on a run carrying
the knob sweep's real parameter names, with the freeze swapped for a recording fake, check
it releases the run's freeze and logs that on the run, and check the knob sweep registers
it after the hook that destroys the bench endpoint.
"""

import inspect
import logging
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from prefect.client.schemas.objects import FlowRun
from prefect.settings import PREFECT_LOGGING_TO_API_ENABLED, temporary_settings
from prefect.states import Cancelling, Crashed

import slipstream_bench.orchestration.flows.run_setup as run_setup_module
from slipstream_bench.orchestration.cluster import Kubectl
from slipstream_bench.orchestration.flows.knob_sweep import knob_sweep_flow
from slipstream_bench.orchestration.flows.run_setup import (
    destroy_run_bench_endpoint,
    release_run_rollout_freeze,
)
from tests.orchestration.rollout_freeze_fakes import RecordingFreeze


def _kubectl(_args: Any, _stdin: str | None) -> str:
    return ""


@pytest.fixture(autouse=True)
def _keep_run_logs_local() -> Iterator[None]:
    """Keep the hook's run log lines local, so no test sends them to a Prefect API."""
    with temporary_settings({PREFECT_LOGGING_TO_API_ENABLED: False}):
        yield


@pytest.fixture
def freeze(monkeypatch: pytest.MonkeyPatch) -> RecordingFreeze:
    recorder = RecordingFreeze()

    def _build_rollout_freeze(kubectl: Kubectl) -> RecordingFreeze:
        assert kubectl is _kubectl
        return recorder

    monkeypatch.setattr(run_setup_module, "build_kubectl", lambda: _kubectl)
    monkeypatch.setattr(run_setup_module, "build_rollout_freeze", _build_rollout_freeze)
    return recorder


def _flow_run(parent_flow: Any) -> FlowRun:
    """A run of ``parent_flow`` whose parameters carry the flow's own parameter names."""
    names = inspect.signature(parent_flow.fn).parameters
    return FlowRun(
        flow_id=uuid4(),
        parameters={
            name: "run-7" if name == "run_id" else f"<{name}>" for name in names
        },
    )


@pytest.mark.parametrize("state", [Cancelling(), Crashed()])
def test_hook_releases_the_run_s_freeze(freeze: RecordingFreeze, state: Any) -> None:
    release_run_rollout_freeze(
        flow=knob_sweep_flow, flow_run=_flow_run(knob_sweep_flow), state=state
    )

    assert freeze.calls == [("release", "run-7")]


def test_hook_logs_the_release_on_the_run(
    freeze: RecordingFreeze, caplog: pytest.LogCaptureFixture
) -> None:
    release_run_rollout_freeze(
        flow=knob_sweep_flow, flow_run=_flow_run(knob_sweep_flow), state=Cancelling()
    )

    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "prefect.flow_runs"
    ] == [
        (logging.INFO, "releasing run run-7's rollout freeze"),
        (logging.INFO, "released run run-7's rollout freeze"),
    ]


def test_knob_sweep_releases_its_freeze_after_destroying_its_endpoint() -> None:
    # The release lets Argo CD roll the worker, which would cut off a destroy still
    # running in it, so it runs last.
    hooks = [destroy_run_bench_endpoint, release_run_rollout_freeze]

    assert knob_sweep_flow.on_cancellation_hooks == hooks
    assert knob_sweep_flow.on_crashed_hooks == hooks
