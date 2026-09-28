"""The orchestration entrypoint: drive one engine point's resumable Tier-2 sweep.

ADR-0012 §Amendment: the driver runs at the control point (a workstation today) with
the ``orchestration`` extra installed; the bench-client image never carries it. The
justfile stays thin and calls ``slipstream-orchestrate point-sweep`` once per engine
point (the Tier-1 GPU redeploy stays in bash) — this command reads the point's context,
folds the deep config digest, wires the S3-backed cell task, and runs the flow so an
interrupted run resumes at cell granularity.

Kept its own console script (not on the Prefect-free ``slipstream-bench`` app) because
its whole job needs Prefect and boto3, present only in the orchestration extra.
"""

from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.orchestration.cell_run import build_s3_client
from slipstream_bench.orchestration.cell_task import cell_task
from slipstream_bench.orchestration.digest import DigestInputs
from slipstream_bench.orchestration.flow import SweepContext, run_point_sweep
from slipstream_bench.orchestration.model_config import read_model_id
from slipstream_bench.orchestration.ssm import build_ssm_client

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
    open so ``point-sweep`` stays a subcommand and further commands can be added.
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


if __name__ == "__main__":
    app()
