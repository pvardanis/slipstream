"""Composition root for the L0 benchmark reporting surface.

Assembles the sweep sub-app (``sweep-grid``) and the report member's sub-app
(``aggregate-sweep``/``report``/``chart``) onto one root ``slipstream-bench-report`` app.
This is the transitional root -> report edge ADR-0017 carves in two steps: the entry point
stays in the root member here (#231) and moves into report when #232 dissolves the root.
The per-cell executor, cost post-processors, prefix-cache scraper, and leak check belong to
the executor member's own CLI (``slipstream_bench.executor.cli``, ADR-0017).
"""

import typer
from slipstream_bench.report.cli import app as report_app

from slipstream_bench.sweep.cli import app as sweep_app

app = typer.Typer(
    name="slipstream-bench-report",
    help="L0 benchmark reporting: aggregate sweeps and render the cost/latency report.",
    no_args_is_help=True,
    add_completion=False,
)

# A nameless, callback-less sub-app merges its commands onto the root at the same
# level, so each concept groups its commands in its own module while the CLI keeps
# a flat command surface (`slipstream-bench-report aggregate-sweep`, not `... sweep ...`).
app.add_typer(sweep_app)
app.add_typer(report_app)


if __name__ == "__main__":
    app()
