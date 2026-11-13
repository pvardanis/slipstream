"""The cold-start-breakdown driver: one true-zero cold start, cycled and read (ADR-0030).

:func:`drive_cold_start` is the transport-free cycle. It scales the GPU replica to zero,
waits until Karpenter has removed every GPU NodeClaim, scales the replica back to one,
waits for its pod to go Ready, and reads the pod-created → Ready total and its
cluster-side phases. Whether the run passes or fails, it scales the replica back to zero
last, so no run leaves a GPU node up. Its collaborators are injected as callables, so the
whole cycle is testable against a fake ``Kubectl`` with no cluster.

:func:`build_cold_start_collaborators` binds those callables to kubectl.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from slipstream_bench.contract.cold_start_record import ColdStartBreakdown
from slipstream_bench.orchestration.cluster import Kubectl
from slipstream_bench.orchestration.cold_start_cluster import (
    read_cold_start_breakdown,
    scale_replica,
    wait_for_no_gpu_nodeclaim,
    wait_for_replica_ready,
)


@dataclass(frozen=True)
class ColdStartCollaborators:
    """The in-cluster callables the driver sequences.

    :param scale_replica: set the GPU replica's pod count.
    :param wait_for_no_gpu_nodeclaim: wait until the GPU pool holds no NodeClaim.
    :param wait_for_replica_ready: wait until the replica's pod is Ready.
    :param read_breakdown: read the Ready pod's cold start as the given run.
    """

    scale_replica: Callable[[int], None]
    wait_for_no_gpu_nodeclaim: Callable[[], None]
    wait_for_replica_ready: Callable[[], None]
    read_breakdown: Callable[[int], ColdStartBreakdown]


def drive_cold_start(
    collaborators: ColdStartCollaborators, *, run: int
) -> ColdStartBreakdown:
    """Cycle the replica through zero, read its cold start, and leave it at zero.

    :param collaborators: the callables the cycle is sequenced through.
    :param run: the run's 1-based number within its measurement.
    :return: the run's total and its cluster-side phases.
    :raise ColdStartCycleError: when a GPU NodeClaim outlives the scale to zero.
    :raise ColdStartMarkerError: when the pod is never Ready or a marker is missing.
    """
    try:
        collaborators.scale_replica(0)
        collaborators.wait_for_no_gpu_nodeclaim()
        collaborators.scale_replica(1)
        collaborators.wait_for_replica_ready()
        return collaborators.read_breakdown(run)
    finally:
        collaborators.scale_replica(0)


def build_cold_start_collaborators(
    kubectl: Kubectl,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> ColdStartCollaborators:
    """Bind the driver's collaborators to kubectl.

    :param kubectl: the transport the scales, waits and reads are issued through.
    :param sleep: the wait between NodeClaim polls, injected for tests.
    :param now: the monotonic clock the NodeClaim wait times out on, injected for tests.
    :return: the collaborators :func:`drive_cold_start` sequences.
    """
    return ColdStartCollaborators(
        scale_replica=partial(scale_replica, kubectl),
        wait_for_no_gpu_nodeclaim=partial(
            wait_for_no_gpu_nodeclaim, kubectl, sleep=sleep, now=now
        ),
        wait_for_replica_ready=partial(wait_for_replica_ready, kubectl),
        read_breakdown=lambda run: read_cold_start_breakdown(kubectl, run=run),
    )
