"""The cold-start-breakdown driver: true-zero cold starts, cycled and read (ADR-0030).

:func:`drive_cold_start` is the transport-free cycle. It scales the GPU replica to zero,
waits until Karpenter has removed every GPU NodeClaim, scales the replica back to one,
waits for its pod to go Ready, and reads the pod-created → Ready total and its phases.
Whether the run passes or fails, it scales the replica back to zero last, so no run
leaves a GPU node up. Its collaborators are injected as callables, so the whole cycle is
testable against a fake ``Kubectl`` with no cluster.

:func:`build_cold_start_collaborators` binds those callables to kubectl, and
:func:`record_cold_start` drives the cycle once per run inside the run's rollout freeze
(ADR-0029), each run from a verified zero, then writes each run's breakdown under the
results bucket's ``cold-start`` root, publishes each phase's median and range across the
runs to the run page, and uploads the runs' stacked-bar chart. A flow run that fails
partway records nothing and is re-run whole. :func:`cold_start_flow` is the ``@flow``
composition root the Prefect deployment registers. No requests are sent and no bench
endpoint is opened: the run measures the cluster alone. A killed run skips the driver's
own scale to zero, so the flow registers :func:`scale_run_replica_to_zero` as a crash,
cancellation and timeout hook, ahead of the hook that releases the freeze.
"""

import json
import logging
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
from slipstream_bench.orchestration.gpu_roles import (
    SERVING_FLEET,
    refuse_while_role_holds_gpus,
)
from slipstream_bench.orchestration.tasks.render import upload_plot
from slipstream_bench.report.cold_start_aggregation import summarize_cold_starts
from slipstream_bench.report.cold_start_chart import cold_start_spreads_to_markdown
from slipstream_bench.report.cold_start_plotters import plot_cold_start_png

# Reaches the run page through the package logger milestone logging lifts (ADR-0020).
_LOGGER = logging.getLogger(__name__)
# The results bucket's root for the cold-start experiment (ADR-0024).
_RECORD_ROOT = "cold-start"
_ARTIFACT_KEY = "cold-start-breakdown"
# The chart's object name in the run's charts prefix.
_PLOT_NAME = "cold-start-breakdown.png"
# The cold starts a run measures by default, so one slow pull or capacity gap does not
# decide the median, and the most it may measure, which bounds its timeout.
_DEFAULT_RUNS = 3
_MAX_RUNS = 5
# Each cold start's share of the run's timeout: above the cycle's longest legitimate wait
# (a 15 min node drain, then a 20 min rollout).
_CYCLE_TIMEOUT_S = 3600
# The run's timeout, which its rollout freeze lasts too: an hour for each cold start the
# run may measure, so a hung run fails, and a freeze no hook released lapses, within five
# hours. Prefect fixes a flow's timeout when the flow is defined, so it is sized for the
# most runs rather than the run count a run is given.
_RUN_TIMEOUT_S = _MAX_RUNS * _CYCLE_TIMEOUT_S


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


# The run's crash, cancellation and timeout hooks, in the order they run: the scale to
# zero before the release, since the release lets Argo CD roll the worker the scale runs
# in.
@flow(
    name="cold-start-breakdown",
    timeout_seconds=_RUN_TIMEOUT_S,
    on_cancellation=[scale_run_replica_to_zero, release_run_rollout_freeze],
    on_crashed=[scale_run_replica_to_zero, release_run_rollout_freeze],
    on_failure=[
        on_run_timeout(scale_run_replica_to_zero),
        on_run_timeout(release_run_rollout_freeze),
    ],
)
def cold_start_flow(
    *, run_id: str, region: str, bucket: str, runs: int = _DEFAULT_RUNS
) -> str:
    """Measure ``runs`` true-zero cold starts' phases as one Prefect flow.

    The composition root the deployment registers: it binds the driver's collaborators
    to kubectl and drives the cycle once per run inside the run's rollout freeze, then
    records each run's breakdown under ``cold-start/<run-id>/``, publishes the phases'
    spread to the run page and uploads the runs' chart. The replica is left at zero
    whether the run passes or fails. The run is tagged ``run=<run-id>``; it times out
    after five hours, and its failure hooks then scale the replica to zero and release
    the freeze.

    :param run_id: the run id the records nest under and the freeze is held for.
    :param region: the AWS region of the results bucket.
    :param bucket: the results bucket (RESULTS_BUCKET).
    :param runs: the cold starts to measure, 1 to 5.
    :return: the ``s3://`` URI of the prefix the records nest under.
    :raise ValueError: when ``runs`` is not between 1 and 5.
    """
    logger = get_run_logger()
    enable_milestone_logging()
    tag_run_with_group(run_id)
    logger.info("cold-start run=%s starting", run_id)
    kubectl = build_kubectl()
    # The serving fleet shares the GPU quota, so the run refuses to start while it has
    # replicas, before it touches the cluster or the bucket (ADR-0031).
    refuse_while_role_holds_gpus(kubectl, SERVING_FLEET)
    pointer = record_cold_start(
        run_id=run_id,
        bucket=bucket,
        runs=runs,
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
    :return: the run's total, its six phases and engine init's detail.
    :raise ColdStartCycleError: when a GPU NodeClaim outlives the scale to zero, or
        the engine's log shows a compile cache hit.
    :raise ColdStartMarkerError: when the pod is never Ready or a marker is missing.
    """
    # Each phase is logged as it starts, so the run page shows a long drain or rollout
    # wait in progress rather than a silent run.
    try:
        collaborators.scale_replica(0)
        _LOGGER.info(
            "scaled the GPU replica to 0; waiting until no GPU NodeClaim is left"
        )
        collaborators.wait_for_no_gpu_nodeclaim()
        collaborators.scale_replica(1)
        _LOGGER.info(
            "no GPU NodeClaim is left; scaled the GPU replica to 1, waiting for its "
            "pod to go Ready"
        )
        collaborators.wait_for_replica_ready()
        _LOGGER.info("the GPU replica's pod is Ready; reading its cold start")
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
    runs: int,
    collaborators: ColdStartCollaborators,
    freeze: AbstractContextManager[None],
    s3_client: Any,
    publish_artifact: ArtifactPublisher,
) -> str:
    """Drive ``runs`` cold starts inside the run's freeze, then record and publish them.

    Each run's breakdown is logged as it is measured, so a later run or a write that
    fails still leaves the measurements in the run's log. The records are written once
    the freeze is released and the replica is back at zero, so a write that fails leaves
    no GPU node up; a flow run that fails partway records nothing, since it is re-run
    whole rather than resumed.

    :param run_id: the run id the records nest under.
    :param bucket: the results bucket.
    :param runs: the cold starts to measure, 1 to 5.
    :param collaborators: the callables the cycle is sequenced through.
    :param freeze: the run's rollout freeze session, held for the cycles.
    :param s3_client: the boto3 S3 client the records and chart are written with.
    :param publish_artifact: the markdown-artifact publisher, called with ``key`` and
        ``markdown``.
    :return: the ``s3://`` URI of the prefix the records nest under.
    :raise ValueError: when ``runs`` is not between 1 and 5, before the freeze is held.
    :raise ColdStartCycleError: when a GPU NodeClaim outlives the scale to zero, or
        the engine's log shows a compile cache hit.
    :raise ColdStartMarkerError: when the pod is never Ready or a marker is missing.
    """
    if not 1 <= runs <= _MAX_RUNS:
        raise ValueError(
            f"cold-start run count {runs} is not between 1 and {_MAX_RUNS}: the run's "
            f"timeout allows an hour for each of at most {_MAX_RUNS} cold starts"
        )

    with freeze:
        breakdowns = [
            _measure_cold_start(collaborators, run_id=run_id, run=run)
            for run in range(1, runs + 1)
        ]

    prefix = f"{_RECORD_ROOT}/{run_id}"
    pointer = f"s3://{bucket}/{prefix}/"
    # Rendered before the first write, so breakdowns the summary or chart refuse leave
    # nothing recorded.
    markdown = _summary_to_artifact_markdown(breakdowns, run_id=run_id, pointer=pointer)
    chart = plot_cold_start_png(breakdowns, run_id=run_id)
    _LOGGER.info("cold-start run=%s writing %d records to %s", run_id, runs, pointer)
    for breakdown in breakdowns:
        s3_client.put_object(
            Bucket=bucket,
            Key=f"{prefix}/r{breakdown.run}.json",
            Body=json.dumps(breakdown.to_record()).encode("utf-8"),
            ContentType="application/json",
        )
    publish_artifact(key=_ARTIFACT_KEY, markdown=markdown)
    upload_plot(
        run_prefix=prefix,
        bucket=bucket,
        s3_client=s3_client,
        name=_PLOT_NAME,
        data=chart,
    )
    return pointer


def _measure_cold_start(
    collaborators: ColdStartCollaborators, *, run_id: str, run: int
) -> ColdStartBreakdown:
    """Drive one run's cold start and log its breakdown as soon as it is measured."""
    breakdown = drive_cold_start(collaborators, run=run)
    _LOGGER.info(
        "cold-start run=%s r%d measured %s", run_id, run, breakdown.to_record()
    )
    return breakdown


def _summary_to_artifact_markdown(
    breakdowns: list[ColdStartBreakdown], *, run_id: str, pointer: str
) -> str:
    """Render the runs' phase spreads under the artifact's heading and blurb."""
    summary = summarize_cold_starts(breakdowns)
    lines = [
        "## Cold-start breakdown",
        "",
        (
            f"{summary['runs']} runs of {run_id}, pod created → Ready (ADR-0030): each "
            f"phase's median and, bracketed, its range across the runs; recorded under "
            f"{pointer}."
        ),
        "",
        cold_start_spreads_to_markdown(summary),
    ]
    return "\n".join(lines) + "\n"
