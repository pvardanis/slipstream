"""The knob-sweep flow: the two-tier sweep's parent flow and its sequencing driver (ADR-0015).

Three layers, inner to outer. :func:`drive_knob_sweep` is the transport-free outer loop: it
iterates the grid's engine points and, per point, redeploys the GPU, scrapes the concurrency
ceiling, then runs the per-point
:func:`slipstream_bench.orchestration.flows.point_sweep.run_point_sweep` as a nested subflow.
A resume skips a point whose cells already hold valid measurements (the ~20-minute GPU
redeploy is not re-paid to run zero cells), and a ceiling scrape that finds nothing raises
loudly rather than running the point's ladder against a garbage ceiling. Its collaborators —
the GPU redeploy, the ceiling scrape, the per-point sweep, and the redeploy-skip predicate —
are injected as callables, so the whole path is testable against fakes with no cluster, GPU,
or Prefect server.

:func:`build_knob_sweep_collaborators` binds those four callables from the live transports,
and :func:`knob_sweep_flow` is the ``@flow``-decorated composition root the Prefect
deployment registers: it builds the real collaborators inside the flow and drives the loop,
so only serializable parameters cross the flow boundary. Each point sweep runs nested under
that one parent flow run, so the point sweeps of a knob sweep share a parent->child lineage
in the Prefect UI, and the parent run is tagged ``run=<run-id>`` to join the same ``run=``
filter its point sweeps group under.
"""

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from prefect import Task, flow
from prefect.artifacts import create_markdown_artifact
from prefect.client.orchestration import get_client
from prefect.runtime import flow_run

from slipstream_bench.contract import (
    EnginePoint,
    SweepGrid,
    list_engine_points,
    load_grid,
)
from slipstream_bench.orchestration.cell_run import build_s3_client
from slipstream_bench.orchestration.cluster import (
    Kubectl,
    build_kubectl,
    deploy_gpu_point,
    scrape_ceiling,
)
from slipstream_bench.orchestration.completion import point_is_complete
from slipstream_bench.orchestration.config_artifact import (
    ImageRefs,
    publish_config_artifact,
)
from slipstream_bench.orchestration.digest import DigestInputs
from slipstream_bench.orchestration.flows.point_sweep import (
    SweepContext,
    run_point_sweep,
)
from slipstream_bench.orchestration.model_config import read_model_id
from slipstream_bench.orchestration.render import render_result_tables
from slipstream_bench.orchestration.ssm import build_ssm_client
from slipstream_bench.orchestration.tasks.cell import cell_task

_LOGGER = logging.getLogger(__name__)

# The GPU redeploy for one engine point's knobs (Tier-1). Returns nothing: the flow
# sequences it for its side effect, the rollout of ``deploy/vllm-gpu``.
DeployFn = Callable[[EnginePoint], None]

# The concurrency-ceiling scrape run after a redeploy. Returns nothing: the flow sequences
# it for its raise-on-empty side effect and raises CeilingScrapeError when the engine
# reported none (ADR-0015 fail-loud). The grid fixes the ladder shape, so the scraped
# value is recorded by the task, not threaded through the flow.
ScrapeFn = Callable[[EnginePoint], None]

# One engine point's Tier-2 sweep (``run_point_sweep`` in production), returning the S3
# pointer for each of its cells.
PointSweepFn = Callable[[EnginePoint], list[str]]

# The redeploy-skip gate: whether the point still has cells to run (ADR-0015). False
# when every cell already holds a valid measurement, so the point is skipped entirely.
PendingCellsFn = Callable[[EnginePoint], bool]


@dataclass(frozen=True)
class KnobSweepInputs:
    """The per-run inputs every engine point of one knob sweep shares (ADR-0015).

    The grid and manifest the points are enumerated and redeployed from, the shared run
    the points nest under, and the deep config digest and addressing every point sweep
    keys its cells by. A point's own values — its slug, its nested run id — are derived
    per point from these; nothing here varies point to point.
    """

    grid: SweepGrid
    grid_path: Path
    manifest_path: Path
    run: str
    digest: str
    instance_id: str
    image_ref: str
    bucket: str
    model: str
    commercial: bool = False
    sweep_args_b64: str = ""

    def __post_init__(self) -> None:
        """Reject inputs that would address a run with an empty key.

        Frozen, so validating on construction makes the value valid for its whole life.
        The keying and addressing fields must be non-empty, so a misconfigured sweep
        fails here, before its first ~20-minute GPU redeploy, rather than at the S3 probe
        or silently (an empty ``run`` would pass ``SweepContext``'s own check yet key
        cells under ``sweeps//<slug>/``). The two file paths are checked to exist by the
        command that reads them.
        """
        for name in ("run", "digest", "instance_id", "image_ref", "bucket", "model"):
            if not getattr(self, name).strip():
                raise ValueError(f"KnobSweepInputs.{name} must be a non-empty string")


@flow(name="knob-sweep")
def knob_sweep_flow(
    *,
    run_id: str,
    instance_id: str,
    region: str,
    bucket: str,
    image_ref: str,
    model_yaml: Path,
    sweep_grid: Path,
    vllm_manifest: Path,
    retries: int = 0,
    commercial: bool = False,
    sweep_args_b64: str = "",
) -> list[str]:
    """Drive the whole two-tier knob sweep as one Prefect flow (ADR-0015).

    The composition root and the flow the deployment registers: it enumerates the grid's
    engine points, wires the real in-cluster and per-point collaborators from the live
    transports, and sequences them so one flow run redeploys the GPU, scrapes the ceiling,
    and runs each point's Tier-2 ladder — each point sweep nested under this parent run. A
    resume re-invoked with the same inputs skips a point whose cells already hold valid
    measurements, so its ~20-minute redeploy and scrape are not re-paid. After the loop, a
    terminal task folds the whole run off S3 and renders its ceiling and goodput-cliff tables
    to this parent run page as markdown artifacts (ADR-0018).

    The parameters are all serializable (Prefect persists them through
    ``serialize_parameters``); the live handles are built inside the flow, never passed in.
    The three config paths are read on the worker at run time, so they name in-image files.
    This parent run is tagged ``run=<run-id>``, the same tag its point sweeps group under, so
    the UI filters the whole sweep — parent and points — as one run.

    :param run_id: shared run the points nest under (``<run-id>/<point-slug>``).
    :param instance_id: bench host the cells run on.
    :param region: AWS region of the host and bucket.
    :param bucket: results bucket (RESULTS_BUCKET).
    :param image_ref: bench-client image reference the cells run.
    :param model_yaml: model.yaml — the served model id and a digest input.
    :param sweep_grid: sweep-grid.yaml — the engine points, their cells, a digest input.
    :param vllm_manifest: k8s/vllm-gpu.yaml — the redeploy template and serving image ref.
    :param retries: opt-in cell retries for a transient transport fault.
    :param commercial: run the commercial arm (needs a tokenizer).
    :param sweep_args_b64: extra load-cell flags, base64-encoded.
    :return: the S3 pointer for each cell of every point, in order.
    """
    _tag_run_with_group(run_id)
    grid = load_grid(sweep_grid)
    digest_inputs = DigestInputs(
        model_yaml=model_yaml,
        sweep_grid=sweep_grid,
        vllm_manifest=vllm_manifest,
    )
    inputs = KnobSweepInputs(
        grid=grid,
        grid_path=sweep_grid,
        manifest_path=digest_inputs.vllm_manifest,
        run=run_id,
        digest=digest_inputs.digest(),
        instance_id=instance_id,
        image_ref=image_ref,
        bucket=bucket,
        model=read_model_id(model_yaml),
        commercial=commercial,
        sweep_args_b64=sweep_args_b64,
    )
    # Surface the run's configuration on its Prefect run page: the version anchors (the
    # bench-client and orchestration image content-sha tags, the deep digest), the two
    # verbatim config bodies (model.yaml, sweep-grid.yaml), the manifest's serving image ref,
    # and the vLLM container args verbatim, so an operator inspects exactly what the run
    # executed without the files leaving the image. The orchestration image bakes its own ref
    # as ORCH_IMAGE_REF; a local or test build bakes none, so it reads as "unknown".
    publish_config_artifact(
        run_id=run_id,
        images=ImageRefs(
            bench=image_ref,
            orch=os.environ.get("ORCH_IMAGE_REF") or "unknown",
        ),
        digest_inputs=digest_inputs,
        publish=create_markdown_artifact,
    )
    s3_client = build_s3_client(region)
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = (
        build_knob_sweep_collaborators(
            inputs,
            kubectl=build_kubectl(),
            ssm_client=build_ssm_client(region),
            s3_client=s3_client,
            task=cell_task(bucket, retries=retries),
        )
    )
    pointers = drive_knob_sweep(
        points=list_engine_points(grid),
        deploy_fn=deploy_fn,
        scrape_fn=scrape_fn,
        point_sweep_fn=point_sweep_fn,
        has_pending_cells=has_pending_cells,
    )
    # The point loop has persisted every cell to S3; render the whole run's ceiling and
    # goodput-cliff tables to this parent run page as a terminal task (ADR-0018). It folds
    # the complete grid off S3 — a resumed sweep's cached points fold in too — so the render
    # is isolated from the persisted cells: a publish or materialize failure fails only the
    # render, and a re-run re-renders off S3 without re-running a cell (no automatic retry).
    render_result_tables(
        grid=grid,
        run_prefix=run_id,
        bucket=bucket,
        s3_client=s3_client,
        model=inputs.model,
        publish=create_markdown_artifact,
    )
    return pointers


def drive_knob_sweep(
    *,
    points: Sequence[EnginePoint],
    deploy_fn: DeployFn,
    scrape_fn: ScrapeFn,
    point_sweep_fn: PointSweepFn,
    has_pending_cells: PendingCellsFn,
) -> list[str]:
    """Iterate the points, redeploying and scraping each pending one, then sweeping all.

    The redeploy and scrape — the ~20-minute GPU rollout and its ceiling read — are gated
    on the point having pending cells, so a resume never re-pays them to run zero cells
    (ADR-0015). The sweep runs for every point regardless: for a fully-cached point it
    hits the cache and re-executes nothing, returning the point's cached pointers.

    :param points: the grid's engine points, in enumeration order.
    :param deploy_fn: redeploy the GPU for a point's knobs (Tier-1).
    :param scrape_fn: scrape the concurrency ceiling after a redeploy; raises when the
        engine reported none, so the point's ladder never runs against a garbage ceiling.
    :param point_sweep_fn: run one point's Tier-2 cells as a nested subflow.
    :param has_pending_cells: whether a point still has cells to run; a point with none
        skips its redeploy and scrape — the two expensive tasks — but still runs its
        resumable sweep, which returns its cached pointers (resume).
    :return: the S3 pointer for each cell of every point, in order.
    """
    pointers: list[str] = []
    for point in points:
        if has_pending_cells(point):
            deploy_fn(point)
            # Scrape for its raise-on-empty side effect: a point whose engine reported no
            # ceiling must fail before its ladder runs (ADR-0015). The grid fixes the
            # ladder shape, so the scraped value is recorded by the task, not threaded here.
            scrape_fn(point)
        pointers.extend(point_sweep_fn(point))
    return pointers


def build_knob_sweep_collaborators(
    inputs: KnobSweepInputs,
    *,
    kubectl: Kubectl,
    ssm_client: Any,
    s3_client: Any,
    task: Task[..., str],
) -> tuple[DeployFn, ScrapeFn, PointSweepFn, PendingCellsFn]:
    """Bind the four callables the parent knob-sweep flow sequences per engine point.

    The composition root: the real in-cluster and per-point collaborators, with the
    injected transports (kubectl, the boto3 clients, the cell task) closed over so the
    flow itself stays free of live handles. The deploy renders and applies the point's
    Deployment; the scrape reads its predicted ceiling and raises loud on an empty one
    (ADR-0015), logging the value it read (the grid fixes the ladder, so the ceiling is
    recorded, not threaded — ADR-0009); the point sweep runs the point's Tier-2 cells
    under a per-point results subdir and the sweep's nested run id; the pending gate is
    the negation of the redeploy-skip probe, so a fully-valid point skips its redeploy.

    :param inputs: the per-run inputs every point of the sweep shares.
    :param kubectl: the transport the redeploy and ceiling scrape are issued through.
    :param ssm_client: the boto3 SSM client each point sweep sends cells through.
    :param s3_client: the boto3 S3 client the cell downloads and the pending probe read.
    :param task: the cell task each point sweep caches and gates its cells with.
    :return: the (deploy, scrape, point-sweep, has-pending-cells) callables, in the order
        :func:`drive_knob_sweep` takes them.
    """
    deploy_fn: DeployFn = partial(
        deploy_gpu_point,
        grid=inputs.grid,
        manifest_text=inputs.manifest_path.read_text(encoding="utf-8"),
        kubectl=kubectl,
    )
    scrape_fn: ScrapeFn = partial(_scrape_and_log_ceiling, kubectl=kubectl)
    point_sweep_fn: PointSweepFn = partial(
        _sweep_one_point,
        inputs=inputs,
        ssm_client=ssm_client,
        s3_client=s3_client,
        task=task,
    )
    has_pending_cells: PendingCellsFn = partial(
        _point_has_pending_cells, inputs=inputs, s3_client=s3_client
    )
    return deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells


def _scrape_and_log_ceiling(point: EnginePoint, *, kubectl: Kubectl) -> None:
    """Scrape the point's predicted concurrency ceiling and record it in the log.

    The grid fixes the Tier-2 ladder, so the ceiling is recorded, not threaded into it
    (ADR-0009); the scrape raises loud on an empty ceiling (ADR-0015).
    """
    ceiling = scrape_ceiling(point, kubectl=kubectl)
    _LOGGER.info("point %s predicted concurrency ceiling: %s", point.slug(), ceiling)


def _sweep_one_point(
    point: EnginePoint,
    *,
    inputs: KnobSweepInputs,
    ssm_client: Any,
    s3_client: Any,
    task: Task[..., str],
) -> list[str]:
    """Run one point's Tier-2 ladder under its own nested run id."""
    return run_point_sweep(
        grid_path=inputs.grid_path,
        context=_build_point_context(inputs, point),
        ssm_client=ssm_client,
        s3_client=s3_client,
        task=task,
    )


def _point_has_pending_cells(
    point: EnginePoint, *, inputs: KnobSweepInputs, s3_client: Any
) -> bool:
    """Report whether a point still has cells to run — the redeploy-skip probe negated."""
    return not point_is_complete(
        point,
        grid=inputs.grid,
        run_prefix=inputs.run,
        bucket=inputs.bucket,
        s3_client=s3_client,
        model=inputs.model,
        commercial=inputs.commercial,
    )


def _build_point_context(inputs: KnobSweepInputs, point: EnginePoint) -> SweepContext:
    """Derive one engine point's sweep context, nesting it under the shared run."""
    return SweepContext(
        run_id=f"{inputs.run}/{point.slug()}",
        point_slug=point.slug(),
        digest=inputs.digest,
        instance_id=inputs.instance_id,
        image_ref=inputs.image_ref,
        bucket=inputs.bucket,
        model=inputs.model,
        commercial=inputs.commercial,
        sweep_args_b64=inputs.sweep_args_b64,
    )


def _tag_run_with_group(run_id: str) -> None:
    """Tag the running knob-sweep flow run ``run=<run-id>``, matching its point sweeps.

    The point sweeps tag their own runs through the ``tags`` context manager
    (:func:`slipstream_bench.orchestration.flows.point_sweep.run_point_sweep`); the parent
    run is already created when this flow body runs, so that context manager cannot reach it
    — its run is updated through the API instead. The update overwrites tags rather than
    merging, so the current set is read and the run tag added to it. A direct call outside a
    flow run (no runtime id) is a no-op.
    """
    flow_run_id = flow_run.id
    if flow_run_id is None:
        return
    with get_client(sync_client=True) as client:
        client.update_flow_run(flow_run_id, tags=set(flow_run.tags) | {f"run={run_id}"})
