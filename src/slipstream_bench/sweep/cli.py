"""The sweep concept's Typer sub-app: run, aggregate, and emit the grid.

Owns the ``load-sweep``, ``load-cell``, ``aggregate-sweep``, and ``sweep-grid``
commands, plus the api-key resolver and cell runner that guard them. ``load-cell``
runs the single cell it is handed — one ``vllm bench serve`` per invocation, the
grain the container executes now that the grid loop lives in the orchestration
layer (ADR-0012 §Amendment); ``load-sweep`` still drives the whole grid.
"""

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.results import ResultError
from slipstream_bench.sweep.aggregation import (
    SweepAggregationError,
    aggregate_ceilings,
)
from slipstream_bench.sweep.config import SweepError, load_sweep_config
from slipstream_bench.sweep.grid import (
    SweepGridError,
    SweepGridPart,
    load_grid,
    render_part,
)
from slipstream_bench.sweep.runner import (
    CellOutcome,
    cell_command,
    ensure_out_dir,
    execute_cell,
    run_sweep,
)

app = typer.Typer()


def resolve_api_key_env(env_var: str) -> dict[str, str]:
    """Read a commercial API key from a named env var for the child process.

    The key is kept in the environment, never on the command line, so it survives
    the dry-run echo and process listings without leaking. It is handed to the
    child as ``OPENAI_API_KEY``, the variable the ``vllm bench serve`` OpenAI
    client backend (``--backend openai``) reads for the ``Authorization: Bearer``
    header.

    :param env_var: the environment variable holding the key.
    :return: the child-process env overlay setting ``OPENAI_API_KEY``.
    :raise SweepError: when the named variable is unset, empty, or whitespace-only,
        each of which would send a blank Bearer header on an unauthenticated run
        rather than fail.
    """
    value = os.environ.get(env_var)
    if not value or not value.strip():
        raise SweepError(
            f"--api-key-env {env_var}: environment variable '{env_var}' is unset "
            f"or empty — export the commercial API key before the sweep"
        )
    return {"OPENAI_API_KEY": value}


def run_cell(command: list[str], *, extra_env: Mapping[str, str] | None = None) -> int:
    """Run one cell's command, returning its exit code.

    A missing ``vllm`` binary is not a per-cell transient — every cell would fail
    the same way — so it fails fast with an actionable message rather than a raw
    traceback that would abort the grid before the tally.

    :param command: the fully assembled cell command.
    :param extra_env: variables overlaid on the inherited environment for the
        child, e.g. a resolved API key; ``None`` inherits the parent unchanged.
    :return: the process exit code.
    :raise SweepError: when the command's binary is not on PATH.
    """
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        return subprocess.run(command, check=False, env=env).returncode
    except FileNotFoundError as error:
        raise SweepError(
            f"cannot run '{command[0]}': not found on PATH — the sweep runs inside "
            f"the baked bench-client image where vllm is installed"
        ) from error


@app.command("load-sweep")
def load_sweep(
    *,
    config: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="The experiment-definition YAML: the grid axes, lengths, SLO, seed.",
        ),
    ] = Path("bench/load-sweep.yaml"),
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible endpoint the sweep targets.")
    ] = "http://localhost:8000",
    model: Annotated[
        str,
        typer.Option(help="Served model id (model.yaml is its source of truth)."),
    ] = "Qwen/Qwen2.5-0.5B-Instruct",
    out_dir: Annotated[
        str, typer.Option(help="Directory for the per-cell result JSON.")
    ] = "bench/results",
    api_key_env: Annotated[
        str | None,
        typer.Option(
            help="Env var holding the commercial API key, sent as OPENAI_API_KEY "
            "(kept off the command line). Omit for an unauthenticated endpoint."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option(help="Print the vllm commands instead of running them.")
    ] = False,
) -> None:
    """Sweep vllm bench serve across the prefix-share x burstiness grid a config
    defines."""
    try:
        # A dry run builds no cells and touches no endpoint, so it does not need
        # the key resolved — preview a commercial sweep without exporting a secret.
        extra_env = (
            resolve_api_key_env(api_key_env) if api_key_env and not dry_run else None
        )
        cfg = load_sweep_config(
            config,
            base_url=base_url,
            model=model,
            out_dir=out_dir,
            commercial=api_key_env is not None,
        )
        code = run_sweep(
            cfg,
            dry_run=dry_run,
            runner=lambda command: run_cell(command, extra_env=extra_env),
            echo=typer.echo,
            warn=lambda line: typer.echo(line, err=True),
        )
    except SweepError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    raise typer.Exit(code=code)


@app.command("load-cell")
def load_cell(
    *,
    share: Annotated[
        int, typer.Option(help="This cell's prefix-share percentage (0..100).")
    ],
    burstiness: Annotated[
        float,
        typer.Option(help="This cell's burstiness (low = bursty, 1.0 = Poisson)."),
    ],
    max_concurrency: Annotated[
        int | None,
        typer.Option(
            help="This cell's in-flight cap (closed-loop); omit for open-loop."
        ),
    ] = None,
    config: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="The experiment-definition YAML: the lengths, SLO, seed (its grid "
            "axes are unused — the cell's coordinate is passed explicitly).",
        ),
    ] = Path("bench/load-sweep.yaml"),
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible endpoint the cell targets.")
    ] = "http://localhost:8000",
    model: Annotated[
        str,
        typer.Option(help="Served model id (model.yaml is its source of truth)."),
    ] = "Qwen/Qwen2.5-0.5B-Instruct",
    out_dir: Annotated[
        str, typer.Option(help="Directory for this cell's result JSON.")
    ] = "bench/results",
    api_key_env: Annotated[
        str | None,
        typer.Option(
            help="Env var holding the commercial API key, sent as OPENAI_API_KEY "
            "(kept off the command line). Omit for an unauthenticated endpoint."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option(help="Print the vllm command instead of running it.")
    ] = False,
) -> None:
    """Run one vllm bench serve cell — the grid coordinate it is handed.

    The container executes one cell per ``docker run`` now that the per-cell loop
    lives in the orchestration layer (ADR-0012 §Amendment): the coordinate
    (``--share``/``--burstiness``/``--max-concurrency``) is passed in, the shared
    knobs (lengths, SLO, seed) come from ``--config``, and one result JSON is
    written. Exits 0 when the cell ran and was stamped, 1 when it failed to run or
    could not be stamped, 2 on an invalid config or unset key.
    """
    try:
        # A dry run builds no cell and touches no endpoint, so it does not need the
        # key resolved — preview a commercial cell without exporting a secret.
        extra_env = (
            resolve_api_key_env(api_key_env) if api_key_env and not dry_run else None
        )
        cfg = load_sweep_config(
            config,
            base_url=base_url,
            model=model,
            out_dir=out_dir,
            commercial=api_key_env is not None,
        )
        if dry_run:
            typer.echo(
                " ".join(
                    cell_command(
                        cfg,
                        share=share,
                        burstiness=burstiness,
                        max_concurrency=max_concurrency,
                    )
                )
            )
            raise typer.Exit(code=0)
        ensure_out_dir(cfg.out_dir)
        outcome = execute_cell(
            cfg,
            share=share,
            burstiness=burstiness,
            max_concurrency=max_concurrency,
            runner=lambda command: run_cell(command, extra_env=extra_env),
            echo=typer.echo,
            warn=lambda line: typer.echo(line, err=True),
        )
    except SweepError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    raise typer.Exit(code=0 if outcome is CellOutcome.OK else 1)


@app.command("aggregate-sweep")
def aggregate_sweep(
    *,
    run_dir: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="The bench/results/<run_id> directory the sweep wrote, one subdir "
            "per engine-knob point.",
        ),
    ],
) -> None:
    """Aggregate a knob-sweep run's saved results into the concurrency-ceiling table.

    Emits one JSON row per (max-num-seqs, kv-cache-dtype, prefix-caching) point and
    prefix-share: the measured ceiling and the {timeout, oom, other} failure
    cohorts. oom and num_preemptions read null — the recipe does not collect the pod
    events and /metrics snapshots they need (ADR-0009).
    """
    try:
        rows = aggregate_ceilings(run_dir)
    except (SweepAggregationError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(rows, indent=2))


@app.command("sweep-grid")
def sweep_grid(
    part: Annotated[
        SweepGridPart,
        typer.Argument(help="Which grid values to emit for the knob-sweep loop."),
    ],
    *,
    grid: Annotated[
        Path, typer.Option(help="The sweep grid YAML the recipe varies its knobs over.")
    ] = Path("bench/sweep-grid.yaml"),
) -> None:
    """Emit a validated slice of the knob-sweep grid for `just knob-sweep` to read.

    `points` prints the Tier-1 engine points as TSV (one redeploy per row, keyed by
    its results-subdir slug), `ladder` the Tier-2 --max-concurrency rungs, and
    `burstiness` the pinned scalar. The whole grid is validated first, so an invalid
    value fails here rather than mid-sweep on a live GPU (ADR-0009).
    """
    try:
        loaded = load_grid(grid)
    except SweepGridError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(render_part(part, loaded))
