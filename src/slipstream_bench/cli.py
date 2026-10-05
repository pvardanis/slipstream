"""Composition root for the L0 benchmark reporting surface.

Assembles the sweep-aggregation and report sub-apps onto one root ``slipstream-bench``
app: the multi-cell aggregation and reporting surface. The per-cell executor, cost
post-processors, prefix-cache scraper, and leak check belong to the bench member's own
CLI (``slipstream.bench.cli``, ADR-0017).
"""

import typer

from slipstream_bench.report.cli import app as report_app
from slipstream_bench.sweep.cli import app as sweep_app

app = typer.Typer(
    name="slipstream-bench",
    help="L0 benchmark reporting: aggregate sweeps and render the cost/latency report.",
    no_args_is_help=True,
    add_completion=False,
)

# A nameless, callback-less sub-app merges its commands onto the root at the same
# level, so each concept groups its commands in its own module while the CLI keeps
# a flat command surface (`slipstream-bench aggregate-sweep`, not `... sweep ...`).
app.add_typer(sweep_app)
app.add_typer(report_app)


if __name__ == "__main__":
    app()
