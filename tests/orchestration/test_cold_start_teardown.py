"""The cold-start-breakdown flow's teardown: a killed run leaves the replica at zero.

A crashed, cancelled or timed-out run skips the driver's own scale to zero, so the flow
registers a hook that scales the replica to zero, ahead of the hook that releases the
run's rollout freeze. These tests call the hook as Prefect does, with only ``Kubectl``
faked, and check the flow registers both in that order.
"""

import inspect
import logging
from collections.abc import Iterator
from uuid import uuid4

import pytest
from prefect.client.schemas.objects import FlowRun
from prefect.settings import PREFECT_LOGGING_TO_API_ENABLED, temporary_settings
from prefect.states import Cancelling, Crashed, State

import slipstream_bench.orchestration.flows.cold_start as cold_start_module
from slipstream_bench.orchestration.flows.cold_start import (
    cold_start_flow,
    scale_run_replica_to_zero,
)
from slipstream_bench.orchestration.flows.run_setup import release_run_rollout_freeze
from tests.orchestration.cold_start_fakes import CyclingCluster


@pytest.fixture(autouse=True)
def _keep_run_logs_local() -> Iterator[None]:
    """Keep the hook's run log lines local, so no test sends them to a Prefect API."""
    with temporary_settings({PREFECT_LOGGING_TO_API_ENABLED: False}):
        yield


def _flow_run() -> FlowRun:
    """A run whose parameters carry the flow's own parameter names."""
    names = inspect.signature(cold_start_flow.fn).parameters
    return FlowRun(
        flow_id=uuid4(),
        parameters={
            name: "cs-7" if name == "run_id" else f"<{name}>" for name in names
        },
    )


@pytest.mark.parametrize("state", [Cancelling(), Crashed()])
def test_hook_scales_the_replica_to_zero(
    monkeypatch: pytest.MonkeyPatch, state: State
) -> None:
    cluster = CyclingCluster()
    monkeypatch.setattr(cold_start_module, "build_kubectl", lambda: cluster)

    scale_run_replica_to_zero(flow=cold_start_flow, flow_run=_flow_run(), state=state)

    assert cluster.steps == ["scale 0"]


def test_hook_logs_the_scale_on_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cold_start_module, "build_kubectl", CyclingCluster)

    scale_run_replica_to_zero(
        flow=cold_start_flow, flow_run=_flow_run(), state=Crashed()
    )

    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "prefect.flow_runs"
    ] == [
        (logging.INFO, "scaling run cs-7's GPU replica to zero"),
        (logging.INFO, "scaled run cs-7's GPU replica to zero"),
    ]


def test_flow_scales_a_killed_run_s_replica_to_zero_before_releasing_its_freeze() -> (
    None
):
    hooks = [scale_run_replica_to_zero, release_run_rollout_freeze]

    assert cold_start_flow.on_cancellation_hooks == hooks
    assert cold_start_flow.on_crashed_hooks == hooks
    # Wrapped, so a run that failed in its own code is not torn down a second time.
    failure_hooks = cold_start_flow.on_failure_hooks
    assert all(inspect.unwrap(hook) is not hook for hook in failure_hooks)
    assert [inspect.unwrap(hook) for hook in failure_hooks] == hooks


def test_flow_is_named_for_its_deployment() -> None:
    assert cold_start_flow.name == "cold-start-breakdown"
