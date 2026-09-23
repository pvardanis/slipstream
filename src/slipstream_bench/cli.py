"""Composition root for the L0 benchmark harness.

Assembles the per-concept Typer sub-apps (sweep, cost, report) onto one root
``slipstream-bench`` app and registers the two standalone-module commands,
``prefix-cache`` and ``zero-leak``, beside them.
"""

import json
from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.cost.cli import app as cost_app
from slipstream_bench.prefix_cache import PrefixCacheError, scrape_prefix_cache
from slipstream_bench.report.cli import app as report_app
from slipstream_bench.results import ResultError
from slipstream_bench.sweep.cli import app as sweep_app
from slipstream_bench.zero_leak import LeakError, find_leaks, read_aws_json

app = typer.Typer(
    name="slipstream-bench",
    help="L0 benchmark harness: serve sweeps, cost-per-1M, and prefix-cache hit rates.",
    no_args_is_help=True,
    add_completion=False,
)

# A nameless, callback-less sub-app merges its commands onto the root at the same
# level, so each concept groups its commands in its own module while the CLI keeps
# a flat command surface (`slipstream-bench serve-sweep`, not `... sweep serve-sweep`).
app.add_typer(sweep_app)
app.add_typer(cost_app)
app.add_typer(report_app)


@app.command("prefix-cache")
def prefix_cache(
    *,
    cache_state: Annotated[
        str, typer.Option(help="Which cache regime this run measured: cold or warm.")
    ],
    metrics_before: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="/metrics text captured just before the run.",
        ),
    ],
    metrics_after: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="/metrics text captured just after the run.",
        ),
    ],
    result: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="The run's vllm bench serve --save-result JSON.",
        ),
    ],
    model: Annotated[
        str | None,
        typer.Option(
            help="model_name label to select (default: the result's model_id)."
        ),
    ] = None,
) -> None:
    """Compute the cold/warm prefix-cache hit-rate delta for a bench run."""
    try:
        record = scrape_prefix_cache(
            metrics_before=metrics_before,
            metrics_after=metrics_after,
            result=result,
            cache_state=cache_state,
            model=model,
        )
    except (PrefixCacheError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(record, indent=2))


@app.command("zero-leak")
def zero_leak(
    *,
    instances: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="aws ec2 describe-instances JSON, tag-filtered to Project=slipstream.",
        ),
    ],
    volumes: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="aws ec2 describe-volumes JSON, tag-filtered to Project=slipstream.",
        ),
    ],
) -> None:
    """Assert cloud-verify teardown left no Project=slipstream resource still billing.

    Exit 0 when spend returned to zero, 1 when a tagged instance or volume
    survives (named on stderr), 2 when the AWS JSON is unreadable — a broken
    query fails loud rather than clearing a still-billing g5.
    """
    try:
        leaks = find_leaks(read_aws_json(instances), read_aws_json(volumes))
    except LeakError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    if leaks:
        for leak in leaks:
            typer.echo(f"leak: {leak.kind} {leak.identifier} ({leak.detail})", err=True)
        typer.echo(f"{len(leaks)} tagged leftover(s) still billing", err=True)
        raise typer.Exit(code=1)
    typer.echo("no tagged leftovers: GPU spend returned to zero")


if __name__ == "__main__":
    app()
