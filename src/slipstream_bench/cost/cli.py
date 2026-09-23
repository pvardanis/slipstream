"""The cost concept's Typer sub-app: price bench results into $/1M tokens.

Owns the ``cost`` (self-hosted) and ``commercial-cost`` commands. Each reads its
price and provenance from a per-command YAML file (``--config``, ADR-0011); the
result files stay path arguments because they exist only after a run.
"""

import json
from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.cost.commercial import (
    CommercialCostError,
    load_commercial_cost_inputs,
    price_commercial_files,
)
from slipstream_bench.cost.self_hosted import (
    CostError,
    load_cost_inputs,
    price_files,
)
from slipstream_bench.results import ResultError

app = typer.Typer()

_FILES = Annotated[
    list[Path],
    typer.Argument(
        exists=True,
        dir_okay=False,
        help="vllm bench serve --save-result JSON files to price.",
    ),
]


@app.command("cost")
def cost(
    files: _FILES,
    *,
    config: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="Provenance YAML: price/hr, output:input ratio, weight checksum, "
            "vLLM version, quant recipe.",
        ),
    ],
) -> None:
    """Price bench results into cost-per-1M input and output tokens."""
    try:
        inputs = load_cost_inputs(config)
        records = price_files(files, inputs)
    except (CostError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(records, indent=2))


@app.command("commercial-cost")
def commercial_cost(
    files: _FILES,
    *,
    config: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="Provenance YAML: api, model, price-quoted-on date, $/1M rates.",
        ),
    ],
) -> None:
    """Price bench results at a commercial API's quoted $/1M rates."""
    try:
        inputs = load_commercial_cost_inputs(config)
        records = price_commercial_files(files, inputs)
    except (CommercialCostError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(records, indent=2))
