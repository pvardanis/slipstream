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

from slipstream_bench.orchestration.digest import config_digest, read_image_ref
from slipstream_bench.orchestration.flow import SweepContext, run_point_sweep

app = typer.Typer(
    name="slipstream-orchestrate",
    help="Drive a resumable engine-point sweep through Prefect (ADR-0012).",
    no_args_is_help=True,
    add_completion=False,
)


def build_sweep_context(
    *,
    run_id: str,
    point_slug: str,
    instance_id: str,
    image_ref: str,
    bucket: str,
    model: str,
    model_yaml: Path,
    sweep_grid: Path,
    vllm_manifest: Path,
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
    :param model_yaml: ``model.yaml`` (model identity), a digest input.
    :param sweep_grid: ``sweep-grid.yaml`` (swept knobs), a digest input.
    :param vllm_manifest: ``k8s/vllm-gpu.yaml``, holding the serving image ref.
    :param commercial: whether this is the commercial arm.
    :param sweep_args_b64: optional extra ``load-cell`` flags, base64-encoded.
    :return: the fully-populated :class:`SweepContext`.
    """
    digest = config_digest(
        model_yaml=model_yaml.read_bytes(),
        sweep_grid_yaml=sweep_grid.read_bytes(),
        image_ref=read_image_ref(vllm_manifest),
    )
    return SweepContext(
        run_id=run_id,
        point_slug=point_slug,
        digest=digest,
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
    config: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, help="The point's SweepConfig YAML."),
    ],
    run_id: Annotated[
        str, typer.Option(help="Point-nested bucket prefix (<run>/<point-slug>).")
    ],
    point_slug: Annotated[str, typer.Option(help="Engine point's cache-key slug.")],
    instance_id: Annotated[str, typer.Option(help="Bench host instance id.")],
    region: Annotated[str, typer.Option(help="AWS region of the host and bucket.")],
    bucket: Annotated[str, typer.Option(help="Results bucket (RESULTS_BUCKET).")],
    image_ref: Annotated[str, typer.Option(help="Bench-client image reference.")],
    model: Annotated[str, typer.Option(help="Served model id (model.yaml SoT).")],
    model_yaml: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, help="model.yaml (digest input)."),
    ],
    sweep_grid: Annotated[
        Path,
        typer.Option(
            exists=True, dir_okay=False, help="sweep-grid.yaml (digest input)."
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
    from slipstream_bench.orchestration.cell_run import build_s3_client
    from slipstream_bench.orchestration.cell_task import cell_task
    from slipstream_bench.orchestration.ssm import build_ssm_client

    context = build_sweep_context(
        run_id=run_id,
        point_slug=point_slug,
        instance_id=instance_id,
        image_ref=image_ref,
        bucket=bucket,
        model=model,
        model_yaml=model_yaml,
        sweep_grid=sweep_grid,
        vllm_manifest=vllm_manifest,
        commercial=commercial,
        sweep_args_b64=sweep_args_b64,
    )
    pointers = run_point_sweep(
        config_path=config,
        results_dir=results_dir,
        context=context,
        ssm_client=build_ssm_client(region),
        s3_client=build_s3_client(region),
        task=cell_task(bucket, retries=retries),
    )
    for pointer in pointers:
        typer.echo(pointer)


if __name__ == "__main__":
    app()
