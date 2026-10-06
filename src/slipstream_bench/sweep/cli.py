"""The sweep concept's Typer sub-app: emit the knob-sweep grid.

Owns the ``sweep-grid`` command: emit a validated slice of the knob-sweep grid for
``just knob-sweep`` to read. The multi-cell aggregation (``aggregate-sweep``) lives in
the report member's CLI (``slipstream_bench.report.cli``), and the per-cell executor
(``load-cell``/``load-sweep``) in the executor member's CLI
(``slipstream_bench.executor.cli``, ADR-0017).
"""

from pathlib import Path
from typing import Annotated

import typer
from slipstream_bench.contract import SweepGridError, load_grid

from slipstream_bench.sweep.grid import SweepGridPart, render_part

app = typer.Typer()


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
