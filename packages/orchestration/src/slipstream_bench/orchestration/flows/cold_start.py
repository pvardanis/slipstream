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
to the run page. :func:`cold_start_flow` is the ``@flow`` composition root the Prefect
deployment registers. No requests are sent and no bench endpoint is opened: the run
measures the cluster alone. A killed run skips the driver's own scale to zero, so the flow
registers :func:`scale_run_replica_to_zero` as a crash, cancellation and timeout hook,
ahead of the hook that releases the freeze.
"""

import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from typing import Any

from prefect import Flow, flow
from prefect.artifacts import create_markdown_artifact
from prefect.client.schemas.objects import FlowRun
from prefect.logging import get_run_logger
from prefect.logging.loggers import flow_run_logger
from prefect.states import State

from slipstream_bench.contract.cold_start_record import ColdStartBreakdown
from slipstream_bench.orchestration.cell_run import build_s3_client
from slipstream_bench.orchestration.cluster import Kubectl, build_kubectl
from slipstream_bench.orchestration.cold_start_cluster import (
    read_cold_start_breakdown,
    scale_replica,
    wait_for_no_gpu_nodeclaim,
    wait_for_replica_ready,
)
from slipstream_bench.orchestration.config_artifact import ArtifactPublisher
from slipstream_bench.orchestration.flows.run_setup import (
    enable_milestone_logging,
    on_run_timeout,
    open_run_rollout_freeze,
    release_run_rollout_freeze,
    tag_run_with_group,
)

# The results bucket's root for the cold-start experiment (ADR-0024).
_RECORD_ROOT = "cold-start"
# One cold start is measured per run id.
_RUN = 1
_ARTIFACT_KEY = "cold-start-breakdown"
# The run's timeout, which its rollout freeze lasts too: above the cycle's longest
# legitimate wait (a 15 min node drain, then a 20 min rollout), so a hung run fails, and a
# freeze no hook released lapses, within the hour.
_RUN_TIMEOUT_S = 3600


def scale_run_replica_to_zero(
    flow: Flow[..., Any], flow_run: FlowRun, state: State
) -> None:
    """Scale a crashed, cancelled or timed-out run's GPU replica to zero: a state hook.

    A killed run's driver skips its own scale to zero, so the hook rebuilds its transport
    from the worker's environment and scales the replica down, logging its start and end
    on the run itself. The flow registers it ahead of the freeze's release, which lets
    Argo CD roll the worker the hook runs in. Prefect's runner passes the arguments by
    keyword and the in-process engine passes them in order, so both their names and their
    order are fixed.

    :param flow: the flow whose run ended, which names the run's log lines.
    :param flow_run: the run, whose ``run_id`` parameter names its log lines.
    :param state: the Cancelling, Crashed or TimedOut state the run entered (unused; part
        of the hook signature Prefect calls).
    :raise KubectlError: when the Deployment cannot be patched.
    """
    logger = flow_run_logger(flow_run, flow)
    run_id = flow_run.parameters["run_id"]
    logger.info("scaling run %s's GPU replica to zero", run_id)
    scale_replica(build_kubectl(), 0)
    logger.info("scaled run %s's GPU replica to zero", run_id)


# The run's crash and cancellation hooks, in the order they run: the scale to zero before
# the release, since the release lets Argo CD roll the worker the scale runs in.
_TEARDOWN_HOOKS = (scale_run_replica_to_zero, release_run_rollout_freeze)


@flow(
    name="cold-start-breakdown",
    timeout_seconds=_RUN_TIMEOUT_S,
    on_cancellation=list(_TEARDOWN_HOOKS),
    on_crashed=list(_TEARDOWN_HOOKS),
    on_failure=[on_run_timeout(hook) for hook in _TEARDOWN_HOOKS],
)
def cold_start_flow(*, run_id: str, region: str, bucket: str) -> str:
    """Measure one true-zero cold start's cluster-side phases as one Prefect flow.

    The composition root the deployment registers: it binds the driver's collaborators
    to kubectl and drives the cycle inside the run's rollout freeze, then records the
    breakdown under ``cold-start/<run-id>/`` and publishes its phase table to the run
    page. The replica is left at zero whether the run passes or fails. The run is tagged
    ``run=<run-id>``; it times out after an hour, and its failure hooks then scale the
    replica to zero and release the freeze.

    :param run_id: the run id the record nests under and the freeze is held for.
    :param region: the AWS region of the results bucket.
    :param bucket: the results bucket (RESULTS_BUCKET).
    :return: the record's ``s3://`` URI.
    """
    logger = get_run_logger()
    enable_milestone_logging()
    tag_run_with_group(run_id)
    logger.info("cold-start run=%s starting", run_id)
    kubectl = build_kubectl()
    pointer = record_cold_start(
        run_id=run_id,
        bucket=bucket,
        collaborators=build_cold_start_collaborators(kubectl),
        freeze=open_run_rollout_freeze(
            kubectl=kubectl, run_id=run_id, duration_s=_RUN_TIMEOUT_S
        ),
        s3_client=build_s3_client(region),
        publish_artifact=create_markdown_artifact,
    )
    logger.info("cold-start run=%s recorded at %s", run_id, pointer)
    return pointer


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
