"""The cost concept's Typer sub-app: price bench results into $/1M tokens.

Owns the ``cost`` (self-hosted) and ``commercial-cost`` commands.
"""

import json
from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.cost.commercial import (
    CommercialCostError,
    CommercialCostInputs,
    price_commercial_files,
)
from slipstream_bench.cost.self_hosted import CostError, CostInputs, price_files
from slipstream_bench.results import ResultError

app = typer.Typer()


@app.command("cost")
def cost(
    files: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="vllm bench serve --save-result JSON files to price.",
        ),
    ],
    *,
    price_per_hour: Annotated[
        float, typer.Option(help="Instance price in USD/hour (on-demand or spot).")
    ],
    output_input_ratio: Annotated[
        float,
        typer.Option(
            help="Price weight of an output token vs an input token (1 = equal)."
        ),
    ],
    weight_checksum: Annotated[
        str, typer.Option(help="Checksum of the weight blob the run served.")
    ],
    vllm_version: Annotated[
        str, typer.Option(help="vLLM version that produced the result.")
    ],
    quant_recipe: Annotated[
        str, typer.Option(help="Quantization recipe (e.g. awq_marlin+fp8-kv).")
    ],
) -> None:
    """Price bench results into cost-per-1M input and output tokens."""
    try:
        inputs = CostInputs(
            price_per_hour=price_per_hour,
            output_input_ratio=output_input_ratio,
            weight_checksum=weight_checksum,
            vllm_version=vllm_version,
            quant_recipe=quant_recipe,
        )
        records = price_files(files, inputs)
    except (CostError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(records, indent=2))


@app.command("commercial-cost")
def commercial_cost(
    files: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="vllm bench serve --save-result JSON files to price.",
        ),
    ],
    *,
    input_price_per_1m: Annotated[
        float, typer.Option(help="Provider's published $/1M input tokens.")
    ],
    output_price_per_1m: Annotated[
        float, typer.Option(help="Provider's published $/1M output tokens.")
    ],
    api: Annotated[str, typer.Option(help="Provider the rate was quoted from.")],
    model: Annotated[str, typer.Option(help="Provider model the rate applies to.")],
    price_quoted_on: Annotated[
        str, typer.Option(help="ISO date the rate was quoted (e.g. 2026-09-11).")
    ],
) -> None:
    """Price bench results at a commercial API's quoted $/1M rates."""
    try:
        inputs = CommercialCostInputs(
            input_price_per_1m=input_price_per_1m,
            output_price_per_1m=output_price_per_1m,
            api=api,
            model=model,
            price_quoted_on=price_quoted_on,
        )
        records = price_commercial_files(files, inputs)
    except (CommercialCostError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(records, indent=2))
