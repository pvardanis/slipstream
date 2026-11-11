"""The flows' timeout hooks: tear a timed-out run down as its crash and cancel hooks do.

Prefect stops a run that outlives its ``timeout_seconds`` by raising a cancellation, a
``BaseException``, into its body, so the run's sessions leave their teardown to its hooks;
the run then ends ``TimedOut``, a failure, which runs only its failure hooks. These tests
check the failure hooks act on a timed-out run alone, that the knob sweep registers the
destroy and then the release there, and, on a real run under a Prefect test server, that a
timed-out run's freeze is released by its hook.
"""

import inspect
import logging
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from prefect import flow
from prefect.client.schemas.objects import FlowRun
from prefect.states import Failed, State, StateType
from prefect.testing.utilities import prefect_test_harness

import slipstream_bench.orchestration.flows.run_setup as run_setup_module
from slipstream_bench.orchestration.flows.knob_sweep import knob_sweep_flow
from slipstream_bench.orchestration.flows.run_setup import (
    destroy_run_bench_endpoint,
    on_run_timeout,
    release_run_rollout_freeze,
)
from slipstream_bench.orchestration.rollout_freeze import hold_rollout_freeze
from tests.orchestration.rollout_freeze_fakes import RecordingFreeze


def _timed_out() -> State:
    return State(type=StateType.FAILED, name="TimedOut")


def _recording_hook(calls: list[str]) -> Any:
    def hook(flow: Any, flow_run: FlowRun, state: State) -> None:
        calls.append(str(state.name))

    return hook


def test_timeout_hook_runs_its_hook_on_a_timed_out_run() -> None:
    calls: list[str] = []

    on_run_timeout(_recording_hook(calls))(
        flow=knob_sweep_flow, flow_run=FlowRun(flow_id=uuid4()), state=_timed_out()
    )

    assert calls == ["TimedOut"]


def test_timeout_hook_leaves_a_run_that_failed_in_its_own_code() -> None:
    # A failure the run raised itself reached its sessions as an Exception, and they
    # tore down then; only a timeout leaves the teardown to the hooks.
    calls: list[str] = []

    on_run_timeout(_recording_hook(calls))(
        flow=knob_sweep_flow, flow_run=FlowRun(flow_id=uuid4()), state=Failed()
    )

    assert calls == []


def test_knob_sweep_tears_a_timed_out_run_down_destroy_first() -> None:
    hooks = knob_sweep_flow.on_failure_hooks

    # Wrapped, so a run that failed in its own code is not torn down a second time.
    assert all(inspect.unwrap(hook) is not hook for hook in hooks)
    assert [inspect.unwrap(hook) for hook in hooks] == [
        destroy_run_bench_endpoint,
        release_run_rollout_freeze,
    ]


@pytest.fixture
def harness() -> Iterator[None]:
    with prefect_test_harness():
        yield


def test_a_timed_out_run_s_freeze_is_released_by_its_hook(
    monkeypatch: pytest.MonkeyPatch,
    harness: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freeze = RecordingFreeze()
    monkeypatch.setattr(run_setup_module, "build_kubectl", lambda: None)
    monkeypatch.setattr(run_setup_module, "build_rollout_freeze", lambda _k: freeze)

    @flow(timeout_seconds=1, on_failure=[on_run_timeout(release_run_rollout_freeze)])
    def outlives_its_timeout(run_id: str) -> None:
        with hold_rollout_freeze(freeze, holder=run_id, duration_s=1):
            time.sleep(10)

    # The wrapped hook releases only on a TimedOut run, so its release shows the run
    # timed out; the session itself released nothing on the cancellation.
    with pytest.raises(TimeoutError):
        outlives_its_timeout(run_id="run-7")

    assert freeze.calls == [("acquire", "run-7", "1"), ("release", "run-7")]
    # The session logged that it left the release to the run's hooks.
    [left] = [
        r.getMessage()
        for r in caplog.records
        if r.name == "slipstream_bench.orchestration.rollout_freeze"
        and r.levelno == logging.ERROR
    ]
    assert left.startswith("rollout freeze not released on CancelledError")
