"""The orchestration entrypoint: drive the knob sweep, or one point's Tier-2 sweep.

Two commands, one console script. ``knob-sweep`` (ADR-0015) drives the whole two-tier
sweep as one parent flow: it enumerates the grid's engine points and, per point,
redeploys the GPU (Tier-1), scrapes the concurrency ceiling, and runs the point's Tier-2
ladder, skipping a point whose cells already hold valid measurements. ``point-sweep``
(ADR-0012 §Amendment) drives a single engine point's resumable Tier-2 ladder — the unit
``knob-sweep`` runs per point, kept its own command for driving one point by hand.

Both read the point context, fold the deep config digest, and wire the S3-backed cell
task so an interrupted run resumes at cell granularity. Kept its own console script (not
on the Prefect-free ``slipstream-bench`` app) because its whole job needs Prefect and
boto3, present only in the orchestration extra.
"""

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import typer
from prefect import Task

from slipstream_bench.orchestration.cell_run import build_s3_client
from slipstream_bench.orchestration.cluster import (
    Kubectl,
    build_kubectl,
    deploy_gpu_point,
    scrape_ceiling,
)
from slipstream_bench.orchestration.completion import point_is_complete
from slipstream_bench.orchestration.digest import DigestInputs
from slipstream_bench.orchestration.flows.knob_sweep import (
    DeployFn,
    PendingCellsFn,
    PointSweepFn,
    ScrapeFn,
    run_knob_sweep,
)
from slipstream_bench.orchestration.flows.point_sweep import (
    SweepContext,
    run_point_sweep,
)
from slipstream_bench.orchestration.model_config import read_model_id
from slipstream_bench.orchestration.ssm import build_ssm_client
from slipstream_bench.orchestration.tasks.cell import cell_task
from slipstream_bench.sweep.aggregation import EnginePoint
from slipstream_bench.sweep.grid import SweepGrid, list_engine_points, load_grid

_LOGGER = logging.getLogger(__name__)

app = typer.Typer(
    name="slipstream-orchestrate",
    help="Drive a resumable engine-point sweep through Prefect (ADR-0012).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _group() -> None:
    """Keep the app a command group so its commands stay named subcommands.

    Typer collapses a single-command app into a nameless root command, which would
    make ``slipstream-orchestrate --run-id ...`` the invocation and reject the
    documented ``slipstream-orchestrate point-sweep ...``. A callback holds the group
    open so ``knob-sweep`` and ``point-sweep`` stay subcommands.
    """


def build_sweep_context(
    *,
    run_id: str,
    point_slug: str,
    instance_id: str,
    image_ref: str,
    bucket: str,
    model: str,
    digest_inputs: DigestInputs,
    commercial: bool = False,
    sweep_args_b64: str = "",
) -> SweepContext:
    """Assemble the point's context, folding the deep config digest from its inputs.

    The digest is ``sha256(model.yaml + sweep-grid.yaml + vLLM image ref)`` (ADR-0012):
    the raw bytes of the two files and the serving image ref read from the vLLM
    manifest, so a model, grid, or image change invalidates the point's cached cells.

    :param run_id: the point-nested bucket prefix (``<run>/<point-slug>``).
    :param point_slug: the engine point's cache-key part.
    :param instance_id: the bench host the cells run on.
    :param image_ref: the bench-client image the cells run.
    :param bucket: the results bucket.
    :param model: the served model id.
    :param digest_inputs: the three files the deep config digest is folded from.
    :param commercial: whether this is the commercial arm.
    :param sweep_args_b64: optional extra ``load-cell`` flags, base64-encoded.
    :return: the fully-populated :class:`SweepContext`.
    """
    return SweepContext(
        run_id=run_id,
        point_slug=point_slug,
        digest=digest_inputs.digest(),
        instance_id=instance_id,
        image_ref=image_ref,
        bucket=bucket,
        model=model,
        commercial=commercial,
        sweep_args_b64=sweep_args_b64,
    )


@app.command("point-sweep")
def point_sweep(
    *,
    run_id: Annotated[
        str, typer.Option(help="Point-nested bucket prefix (<run>/<point-slug>).")
    ],
    point_slug: Annotated[str, typer.Option(help="Engine point's cache-key slug.")],
    instance_id: Annotated[str, typer.Option(help="Bench host instance id.")],
    region: Annotated[str, typer.Option(help="AWS region of the host and bucket.")],
    bucket: Annotated[str, typer.Option(help="Results bucket (RESULTS_BUCKET).")],
    image_ref: Annotated[str, typer.Option(help="Bench-client image reference.")],
    model_yaml: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="model.yaml: the served model id (.model.hfId) and a digest input.",
        ),
    ],
    sweep_grid: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="sweep-grid.yaml: the point's cells and a digest input.",
        ),
    ],
    vllm_manifest: Annotated[
        Path,
        typer.Option(
            exists=True, dir_okay=False, help="k8s/vllm-gpu.yaml (serving image ref)."
        ),
    ],
    results_dir: Annotated[
        Path, typer.Option(help="Local directory each cell's result downloads into.")
    ],
    retries: Annotated[
        int, typer.Option(help="Opt-in cell retries for a transient transport fault.")
    ] = 0,
    commercial: Annotated[
        bool, typer.Option(help="Run the commercial arm (needs a tokenizer).")
    ] = False,
    sweep_args_b64: Annotated[
        str, typer.Option(help="Extra load-cell flags, base64-encoded.")
    ] = "",
) -> None:
    """Run one engine point's Tier-2 cells resumably, printing each cell's S3 pointer.

    Registers the S3 result and cache-key storage blocks, computes the point's digest,
    and drives the flow. An interrupted run re-invoked with the same inputs skips the
    cells that already hold a valid measurement and re-attempts the rest.
    """
    context = build_sweep_context(
        run_id=run_id,
        point_slug=point_slug,
        instance_id=instance_id,
        image_ref=image_ref,
        bucket=bucket,
        model=read_model_id(model_yaml),
        digest_inputs=DigestInputs(
            model_yaml=model_yaml,
            sweep_grid=sweep_grid,
            vllm_manifest=vllm_manifest,
        ),
        commercial=commercial,
        sweep_args_b64=sweep_args_b64,
    )
    cell_result_uris = run_point_sweep(
        grid_path=sweep_grid,
        results_dir=results_dir,
        context=context,
        ssm_client=build_ssm_client(region),
        s3_client=build_s3_client(region),
        task=cell_task(bucket, retries=retries),
    )
    for uri in cell_result_uris:
        typer.echo(uri)


@dataclass(frozen=True)
class KnobSweepInputs:
    """The per-run inputs every engine point of one knob sweep shares (ADR-0015).

    The grid and manifest the points are enumerated and redeployed from, the shared run
    the points nest under, and the deep config digest and addressing every point sweep
    keys its cells by. A point's own values — its slug, its results subdir, its nested
    run id — are derived per point from these; nothing here varies point to point.
    """

    grid: SweepGrid
    grid_path: Path
    manifest_path: Path
    results_dir: Path
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
        :func:`run_knob_sweep` takes them.
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
    """Run one point's Tier-2 ladder under its own results subdir and nested run id."""
    return run_point_sweep(
        grid_path=inputs.grid_path,
        results_dir=inputs.results_dir / point.slug(),
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


@app.command("knob-sweep")
def knob_sweep(
    *,
    run_id: Annotated[
        str,
        typer.Option(help="Shared run the points nest under (<run-id>/<point-slug>)."),
    ],
    instance_id: Annotated[str, typer.Option(help="Bench host instance id.")],
    region: Annotated[str, typer.Option(help="AWS region of the host and bucket.")],
    bucket: Annotated[str, typer.Option(help="Results bucket (RESULTS_BUCKET).")],
    image_ref: Annotated[str, typer.Option(help="Bench-client image reference.")],
    model_yaml: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="model.yaml: the served model id (.model.hfId) and a digest input.",
        ),
    ],
    sweep_grid: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="sweep-grid.yaml: the engine points, their cells, and a digest input.",
        ),
    ],
    vllm_manifest: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="k8s/vllm-gpu.yaml: the redeploy template and the serving image ref.",
        ),
    ],
    results_dir: Annotated[
        Path, typer.Option(help="Local directory each point's cells download under.")
    ],
    retries: Annotated[
        int, typer.Option(help="Opt-in cell retries for a transient transport fault.")
    ] = 0,
    commercial: Annotated[
        bool, typer.Option(help="Run the commercial arm (needs a tokenizer).")
    ] = False,
    sweep_args_b64: Annotated[
        str, typer.Option(help="Extra load-cell flags, base64-encoded.")
    ] = "",
) -> None:
    """Drive the whole two-tier knob sweep unattended, printing every cell's S3 pointer.

    The composition root of the parent flow (ADR-0015): enumerates the grid's engine
    points, wires the real in-cluster and per-point collaborators, and runs the sweep so
    one flow run redeploys the GPU, scrapes the ceiling, and runs each point's Tier-2
    ladder. A resume re-invoked with the same inputs skips a point whose cells already
    hold valid measurements — its ~20-minute redeploy and scrape are not re-paid.
    """
    grid = load_grid(sweep_grid)
    inputs = KnobSweepInputs(
        grid=grid,
        grid_path=sweep_grid,
        manifest_path=vllm_manifest,
        results_dir=results_dir,
        run=run_id,
        digest=DigestInputs(
            model_yaml=model_yaml,
            sweep_grid=sweep_grid,
            vllm_manifest=vllm_manifest,
        ).digest(),
        instance_id=instance_id,
        image_ref=image_ref,
        bucket=bucket,
        model=read_model_id(model_yaml),
        commercial=commercial,
        sweep_args_b64=sweep_args_b64,
    )
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = (
        build_knob_sweep_collaborators(
            inputs,
            kubectl=build_kubectl(),
            ssm_client=build_ssm_client(region),
            s3_client=build_s3_client(region),
            task=cell_task(bucket, retries=retries),
        )
    )
    cell_result_uris = run_knob_sweep(
        points=list_engine_points(grid),
        deploy_fn=deploy_fn,
        scrape_fn=scrape_fn,
        point_sweep_fn=point_sweep_fn,
        has_pending_cells=has_pending_cells,
    )
    for uri in cell_result_uris:
        typer.echo(uri)


if __name__ == "__main__":
    app()
