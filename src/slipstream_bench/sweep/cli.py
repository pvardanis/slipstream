"""The sweep concept's Typer sub-app: aggregate a run and emit the grid.

Owns the ``aggregate-sweep`` and ``sweep-grid`` commands: the first folds a
knob-sweep run's saved results into the concurrency-ceiling table, the second emits
a validated slice of the knob-sweep grid for ``just knob-sweep`` to read. The
per-cell executor (``load-cell``/``load-sweep``) belongs to the bench member's CLI
(``slipstream.bench.cli``, ADR-0017).
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from slipstream.contract import (
    ResultError,
    SweepAggregationError,
    SweepGridError,
    load_grid,
)

from slipstream_bench.sweep.aggregation import aggregate_ceilings
from slipstream_bench.sweep.grid import SweepGridPart, render_part

app = typer.Typer()


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
