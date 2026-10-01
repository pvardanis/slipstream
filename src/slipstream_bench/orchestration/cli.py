"""The orchestration entrypoint: drive the knob sweep, or one point's Tier-2 sweep.

Three commands, one console script. ``knob-sweep`` (ADR-0015) drives the whole two-tier
sweep as one parent flow: it enumerates the grid's engine points and, per point,
redeploys the GPU (Tier-1), scrapes the concurrency ceiling, and runs the point's Tier-2
ladder, skipping a point whose cells already hold valid measurements. ``point-sweep``
(ADR-0012 §Amendment) drives a single engine point's resumable Tier-2 ladder — the unit
``knob-sweep`` runs per point, kept its own command for driving one point by hand.
``register-knob-sweep`` registers ``knob-sweep`` as a Prefect deployment on the process
work pool, the entrypoint the EKS worker runs unattended.

The two sweep commands read the point context, fold the deep config digest, and wire the
S3-backed cell task so an interrupted run resumes at cell granularity. Kept its own
console script (not on the Prefect-free ``slipstream-bench`` app) because its whole job
needs Prefect and boto3, present only in the orchestration extra.
"""

from pathlib import Path
from typing import Annotated

import typer
from prefect.client.orchestration import get_client
from prefect.deployments.runner import EntrypointType
from prefect.exceptions import ObjectNotFound

from slipstream_bench.orchestration.cell_run import build_s3_client
from slipstream_bench.orchestration.digest import DigestInputs
from slipstream_bench.orchestration.flows.knob_sweep import knob_sweep_flow
from slipstream_bench.orchestration.flows.point_sweep import (
    SweepContext,
    run_point_sweep,
)
from slipstream_bench.orchestration.model_config import read_model_id
from slipstream_bench.orchestration.ssm import build_ssm_client
from slipstream_bench.orchestration.tasks.cell import cell_task

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
    """Drive the knob sweep by hand, printing every cell's S3 pointer.

    Runs :func:`knob_sweep_flow` directly against local files — the same flow the EKS
    worker runs from its registered deployment, driven from the workstation for a
    one-off. A resume re-invoked with the same inputs skips a point whose cells already
    hold valid measurements.
    """
    for uri in knob_sweep_flow(
        run_id=run_id,
        instance_id=instance_id,
        region=region,
        bucket=bucket,
        image_ref=image_ref,
        model_yaml=model_yaml,
        sweep_grid=sweep_grid,
        vllm_manifest=vllm_manifest,
        results_dir=results_dir,
        retries=retries,
        commercial=commercial,
        sweep_args_b64=sweep_args_b64,
    ):
        typer.echo(uri)


def _require_work_pool(work_pool: str) -> None:
    """Fail fast if the target work pool is absent, before registering against it.

    ``flow.deploy`` only warns on a missing pool and still returns a deployment id, so a
    typo'd or un-created pool would register a deployment no worker ever polls — every
    triggered run would sit Scheduled forever, the obscure run-time failure the rest of
    this command guards against. Reading the pool over the port-forward raises here with
    an actionable message instead.
    """
    with get_client(sync_client=True) as client:
        try:
            client.read_work_pool(work_pool)
        except ObjectNotFound:
            raise typer.BadParameter(
                f"work pool {work_pool!r} not found; run `just prefect-up` first",
                param_hint="--work-pool",
            ) from None


@app.command("register-knob-sweep")
def register_knob_sweep(
    *,
    region: Annotated[str, typer.Option(help="AWS region of the host and bucket.")],
    bucket: Annotated[str, typer.Option(help="Results bucket (RESULTS_BUCKET).")],
    image_ref: Annotated[str, typer.Option(help="Bench-client image reference.")],
    work_pool: Annotated[
        str, typer.Option(help="Process work pool the sweep worker polls.")
    ] = "sweep-pool",
    model_yaml: Annotated[
        Path, typer.Option(help="In-image path to model.yaml, read on the worker.")
    ] = Path("/app/model.yaml"),
    sweep_grid: Annotated[
        Path, typer.Option(help="In-image path to sweep-grid.yaml, read on the worker.")
    ] = Path("/app/bench/sweep-grid.yaml"),
    vllm_manifest: Annotated[
        Path,
        typer.Option(help="In-image path to k8s/vllm-gpu.yaml, read on the worker."),
    ] = Path("/app/k8s/vllm-gpu.yaml"),
    results_dir: Annotated[
        Path, typer.Option(help="In-image directory each point's cells download under.")
    ] = Path("/app/results"),
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
    """Register the knob-sweep deployment on the process work pool (ADR-0015).

    Creates the deployment the operator triggers with ``prefect deployment run
    knob-sweep/knob-sweep --param run_id=... --param instance_id=...``. The worker runs
    the baked orchestration image, so the entrypoint is stored as a module path resolved
    by import (``EntrypointType.MODULE_PATH``) — no source tree is fetched and no laptop
    path leaks into the deployment — and no image is built (a process worker runs the flow
    in-process from its baked image). The cluster-stable inputs become the deployment's
    default parameters; ``run_id`` and ``instance_id`` are supplied per run at trigger time.

    Run over a port-forward to the in-cluster Prefect API (see ``just prefect-register``).

    The cluster-stable inputs are rejected here if empty: ``run_id`` and ``instance_id``
    are the only parameters ``prefect deployment run`` supplies per run, so an empty
    ``region``, ``bucket``, or ``image_ref`` default would otherwise surface as an obscure
    boto3 failure on the worker at run time, long after registration reported success. The
    target work pool is checked present for the same reason: ``flow.deploy`` only warns on a
    missing pool, so an absent one would register a deployment whose runs never start.

    :param region: AWS region of the host and bucket.
    :param bucket: results bucket (RESULTS_BUCKET).
    :param image_ref: bench-client image reference the cells run.
    :param work_pool: process work pool the sweep worker polls.
    :param model_yaml: in-image path to model.yaml, read on the worker at run time.
    :param sweep_grid: in-image path to sweep-grid.yaml, read on the worker at run time.
    :param vllm_manifest: in-image path to k8s/vllm-gpu.yaml, read on the worker.
    :param results_dir: in-image directory each point's cells download under.
    :param retries: opt-in cell retries for a transient transport fault.
    :param commercial: run the commercial arm (needs a tokenizer).
    :param sweep_args_b64: extra load-cell flags, base64-encoded.
    """
    for name, value in (
        ("region", region),
        ("bucket", bucket),
        ("image-ref", image_ref),
        ("work-pool", work_pool),
    ):
        if not value.strip():
            raise typer.BadParameter(
                "must be a non-empty string", param_hint=f"--{name}"
            )
    _require_work_pool(work_pool)
    deployment_id = knob_sweep_flow.deploy(
        name="knob-sweep",
        work_pool_name=work_pool,
        entrypoint_type=EntrypointType.MODULE_PATH,
        build=False,
        push=False,
        parameters={
            "region": region,
            "bucket": bucket,
            "image_ref": image_ref,
            "model_yaml": str(model_yaml),
            "sweep_grid": str(sweep_grid),
            "vllm_manifest": str(vllm_manifest),
            "results_dir": str(results_dir),
            "retries": retries,
            "commercial": commercial,
            "sweep_args_b64": sweep_args_b64,
        },
    )
    typer.echo(f"registered knob-sweep deployment {deployment_id}")


if __name__ == "__main__":
    app()
