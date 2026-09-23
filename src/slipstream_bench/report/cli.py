"""The report concept's Typer sub-app: the baseline join and the sweep charts.

Owns the ``report`` (baseline $/1M-at-SLO join) and ``chart`` (ceiling and cliff
tables plus plots) commands.
"""

import json
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.report.baseline import (
    ReportError,
    build_report,
    load_records,
    render_markdown,
)
from slipstream_bench.report.chart import write_artifacts
from slipstream_bench.results import ResultError
from slipstream_bench.sweep.aggregation import (
    SweepAggregationError,
    aggregate_ceilings,
    aggregate_rungs,
)

app = typer.Typer()


class ReportFormat(str, Enum):
    """The format the baseline report is emitted in."""

    json = "json"
    markdown = "markdown"


@app.command("report")
def report(
    *,
    self_hosted_cost: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="The `cost` tool's JSON output for the self-hosted arm.",
        ),
    ],
    commercial_cost: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="The `commercial-cost` tool's JSON output for the commercial arm.",
        ),
    ],
    prefix_cache: Annotated[
        list[Path],
        typer.Option(
            exists=True,
            dir_okay=False,
            help="A `prefix-cache` tool JSON record (repeat for cold and warm).",
        ),
    ],
    output_format: Annotated[
        ReportFormat,
        typer.Option("--format", help="Emit the report as JSON or a Markdown table."),
    ] = ReportFormat.json,
) -> None:
    """Join the three arms into the baseline $/1M-at-SLO report."""
    try:
        prefix_cache_records = [
            record for path in prefix_cache for record in load_records(path)
        ]
        rows = build_report(
            self_hosted_cost=load_records(self_hosted_cost),
            commercial_cost=load_records(commercial_cost),
            prefix_cache=prefix_cache_records,
        )
    except ReportError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    if output_format is ReportFormat.markdown:
        typer.echo(render_markdown(rows))
    else:
        typer.echo(json.dumps(rows, indent=2))


@app.command("chart")
def chart(
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
    """Chart a knob-sweep run: write the ceiling and cliff tables and both plots.

    Aggregates the run two ways — the folded ceiling per point and the per-rung goodput
    cliff — then writes each as Markdown and JSON with its plot beside them under
    ``<run_dir>/charts``: the primary ceiling-by-max-num-seqs chart and the diagnostic
    goodput-by-max-concurrency cliff. The tables are the durable artifacts, the PNGs the
    disposable view (ADR-0009).
    """
    charts_dir = run_dir / "charts"
    try:
        rows = aggregate_ceilings(run_dir)
        rungs = aggregate_rungs(run_dir)
        written = write_artifacts(rows, rungs, charts_dir)
    except (SweepAggregationError, ResultError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    except OSError as error:
        typer.echo(
            f"could not write chart artifacts under {charts_dir}: {error}", err=True
        )
        raise typer.Exit(code=2) from error
    for path in written.values():
        typer.echo(str(path))
