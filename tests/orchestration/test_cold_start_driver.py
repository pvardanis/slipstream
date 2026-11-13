"""The cold-start-breakdown driver: one true-zero cold start, cycled and read (ADR-0030).

:func:`drive_cold_start` scales the GPU replica to zero, waits until no GPU NodeClaim is
left, scales it back to one, waits for it to go Ready, reads the breakdown, and leaves the
replica at zero whether the run passes or fails. These tests drive it over its production
collaborators (:func:`build_cold_start_collaborators`) with only the ``Kubectl`` transport
faked, by a cluster that replays the recorded cold start (tests/fixtures/cold-start).
"""

import pytest

from slipstream_bench.contract.cold_start_record import ColdStartBreakdown
from slipstream_bench.orchestration.cold_start import (
    ColdStartCycleError,
    ColdStartMarkerError,
)
from slipstream_bench.orchestration.flows.cold_start import (
    build_cold_start_collaborators,
    drive_cold_start,
)
from tests.orchestration.cold_start_fakes import CyclingCluster, FakeClock


def _drive(
    cluster: CyclingCluster, clock: FakeClock | None = None
) -> ColdStartBreakdown:
    clock = FakeClock() if clock is None else clock
    collaborators = build_cold_start_collaborators(
        cluster, sleep=clock.sleep, now=clock.now
    )
    return drive_cold_start(collaborators, run=1)


def test_the_replica_cycles_through_zero_before_its_cold_start_is_read() -> None:
    cluster = CyclingCluster(drain_polls=2)

    _drive(cluster)

    assert cluster.steps == [
        "scale 0",
        "poll gpu nodeclaims",
        "poll gpu nodeclaims",
        "poll gpu nodeclaims",
        "scale 1",
        "wait ready",
        "get pods",
        "get nodeclaims",
        "get events",
        "scale 0",
    ]


def test_the_cycle_is_issued_within_the_worker_s_rbac() -> None:
    # The worker may patch deploy/vllm-gpu but not its scale subresource, and lists the
    # GPU pool's NodeClaims through its read-only ClusterRole.
    cluster = CyclingCluster(drain_polls=0)

    _drive(cluster)

    assert cluster.calls[:3] == [
        [
            "-n",
            "slipstream",
            "patch",
            "deployment",
            "vllm-gpu",
            "--type=merge",
            "-p",
            '{"spec": {"replicas": 0}}',
        ],
        ["get", "nodeclaims", "-l", "karpenter.sh/nodepool=gpu", "-o", "json"],
        [
            "-n",
            "slipstream",
            "patch",
            "deployment",
            "vllm-gpu",
            "--type=merge",
            "-p",
            '{"spec": {"replicas": 1}}',
        ],
    ]
    assert cluster.calls[3] == [
        "rollout",
        "status",
        "-n",
        "slipstream",
        "deployment/vllm-gpu",
        "--timeout=1200s",
    ]


def test_the_driver_returns_the_recorded_cold_start_s_breakdown() -> None:
    breakdown = _drive(CyclingCluster())

    assert breakdown == ColdStartBreakdown(
        run=1,
        total_s=545.0,
        capacity_wait_s=3.0,
        node_launch_s=47.0,
        image_pull_s=264.0,
    )


def test_a_replica_never_ready_fails_naming_ready_and_is_left_at_zero() -> None:
    cluster = CyclingCluster(ready=False)

    with pytest.raises(
        ColdStartMarkerError,
        match=r"vllm-gpu's pod was not Ready within 1200s.*timed out waiting",
    ):
        _drive(cluster)

    assert cluster.steps[-2:] == ["wait ready", "scale 0"]


def test_a_missing_marker_fails_naming_it_and_leaves_the_replica_at_zero() -> None:
    cluster = CyclingCluster()
    cluster.events["items"] = [
        e for e in cluster.events["items"] if e["reason"] != "Pulled"
    ]

    with pytest.raises(ColdStartMarkerError, match=r"no Pulled event"):
        _drive(cluster)

    assert cluster.steps[-2:] == ["get events", "scale 0"]


def test_a_warm_node_that_outlives_the_drain_wait_fails_naming_it() -> None:
    cluster = CyclingCluster(drain_polls=1000)
    clock = FakeClock()

    with pytest.raises(
        ColdStartCycleError, match=r"GPU NodeClaim gpu-warm remained 900s"
    ):
        _drive(cluster, clock)

    assert set(clock.sleeps) == {10.0}
    assert clock.t == 900.0
    # The replica was never scaled back up, and is left at zero.
    assert "scale 1" not in cluster.steps
    assert cluster.steps[-1] == "scale 0"
