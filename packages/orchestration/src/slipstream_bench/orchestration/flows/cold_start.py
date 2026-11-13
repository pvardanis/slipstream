"""The cold-start-breakdown driver: one true-zero cold start, cycled and read (ADR-0030).

:func:`drive_cold_start` is the transport-free cycle. It scales the GPU replica to zero,
waits until Karpenter has removed every GPU NodeClaim, scales the replica back to one,
waits for its pod to go Ready, and reads the pod-created → Ready total and its
cluster-side phases. Whether the run passes or fails, it scales the replica back to zero
last, so no run leaves a GPU node up. Its collaborators are injected as callables, so the
whole cycle is testable against a fake ``Kubectl`` with no cluster.

:func:`build_cold_start_collaborators` binds those callables to kubectl, and
:func:`record_cold_start` drives the cycle inside the run's rollout freeze (ADR-0029),
then writes the breakdown under the results bucket's ``cold-start`` root and publishes it
to the run page.
"""

import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from typing import Any

from slipstream_bench.contract.cold_start_record import ColdStartBreakdown
from slipstream_bench.orchestration.cluster import Kubectl
from slipstream_bench.orchestration.cold_start_cluster import (
    read_cold_start_breakdown,
    scale_replica,
    wait_for_no_gpu_nodeclaim,
    wait_for_replica_ready,
)
from slipstream_bench.orchestration.config_artifact import ArtifactPublisher

# The results bucket's root for the cold-start experiment (ADR-0024).
_RECORD_ROOT = "cold-start"
# The tracer bullet measures one cold start per run id.
_RUN = 1
_ARTIFACT_KEY = "cold-start-breakdown"


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


def record_cold_start(
    *,
    run_id: str,
    bucket: str,
    collaborators: ColdStartCollaborators,
    freeze: AbstractContextManager[None],
    s3_client: Any,
    publish_artifact: ArtifactPublisher,
) -> str:
    """Drive one cold start inside the run's freeze, then record and publish it.

    The record is written once the freeze is released and the replica is back at zero,
    so a write that fails leaves no GPU node up; a cycle that fails records nothing.

    :param run_id: the run id the record nests under.
    :param bucket: the results bucket.
    :param collaborators: the callables the cycle is sequenced through.
    :param freeze: the run's rollout freeze session, held for the cycle.
    :param s3_client: the boto3 S3 client the record is written with.
    :param publish_artifact: the markdown-artifact publisher, called with ``key`` and
        ``markdown``.
    :return: the record's ``s3://`` URI.
    :raise ColdStartCycleError: when a GPU NodeClaim outlives the scale to zero.
    :raise ColdStartMarkerError: when the pod is never Ready or a marker is missing.
    """
    with freeze:
        breakdown = drive_cold_start(collaborators, run=_RUN)
    key = f"{_RECORD_ROOT}/{run_id}/r{breakdown.run}.json"
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(breakdown.to_record()).encode("utf-8"),
        ContentType="application/json",
    )
    pointer = f"s3://{bucket}/{key}"
    publish_artifact(
        key=_ARTIFACT_KEY,
        markdown=_breakdown_to_markdown(breakdown, run_id=run_id, pointer=pointer),
    )
    return pointer


def _breakdown_to_markdown(
    breakdown: ColdStartBreakdown, *, run_id: str, pointer: str
) -> str:
    """Render the breakdown as the run page's phase table."""
    rows = [
        ("Capacity wait", breakdown.capacity_wait_s),
        ("Node launch", breakdown.node_launch_s),
        ("Image pull", breakdown.image_pull_s),
        ("Unattributed", breakdown.unattributed_s),
    ]
    lines = [
        "## Cold-start breakdown",
        "",
        (
            f"Run {breakdown.run} of {run_id}, pod created → Ready (ADR-0030); "
            f"recorded at {pointer}."
        ),
        "",
        "| Phase | Seconds |",
        "|---|---:|",
        *(f"| {phase} | {seconds} |" for phase, seconds in rows),
        f"| **Total** | **{breakdown.total_s}** |",
    ]
    return "\n".join(lines) + "\n"
