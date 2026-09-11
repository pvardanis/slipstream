"""Front end for the L0 benchmark harness.

Dispatches the serve-sweep, cost, and prefix-cache benchmark subcommands.
"""

import json
from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.cli_helpers import (
    DEFAULT_BURSTINESS,
    DEFAULT_GOODPUT,
    DEFAULT_PREFIX_SHARES,
    run_cell,
    validate_goodput,
    validate_request_rate,
)
from slipstream_bench.cost import CostError, CostInputs, price_files
from slipstream_bench.prefix_cache import PrefixCacheError, scrape_prefix_cache
from slipstream_bench.results import ResultError
from slipstream_bench.serve_sweep import SweepConfig, SweepError, run_sweep

app = typer.Typer(
    name="slipstream-bench",
    help="L0 benchmark harness: serve sweeps, cost-per-1M, and prefix-cache hit rates.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("serve-sweep")
def serve_sweep(
    *,
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible endpoint the sweep targets.")
    ] = "http://localhost:8000",
    model: Annotated[str, typer.Option(help="Served model id.")] = (
        "Qwen/Qwen2.5-0.5B-Instruct"
    ),
    prefix_share: Annotated[
        list[int],
        typer.Option(
            min=0, max=100, help="Prefix-share percentage to sweep (repeatable)."
        ),
    ] = DEFAULT_PREFIX_SHARES,
    burstiness: Annotated[
        list[float],
        typer.Option(
            min=0, help="Burstiness to sweep, low = bursty, 1.0 = Poisson (repeatable)."
        ),
    ] = DEFAULT_BURSTINESS,
    total_len: Annotated[
        int,
        typer.Option(min=1, help="Prefix+suffix token budget, split by prefix-share."),
    ] = 1000,
    num_prompts: Annotated[
        int, typer.Option(min=1, help="Requests per grid cell.")
    ] = 100,
    num_prefixes: Annotated[
        int, typer.Option(min=1, help="Distinct shared prefixes to generate.")
    ] = 5,
    output_len: Annotated[
        int, typer.Option(min=1, help="Output tokens per request.")
    ] = 128,
    align_blocks: Annotated[
        int,
        typer.Option(
            min=0, help="Floor the prefix to a multiple of N tokens (0 = off)."
        ),
    ] = 0,
    request_rate: Annotated[
        str,
        typer.Option(callback=validate_request_rate, help="Requests/sec, or 'inf'."),
    ] = "8",
    seed: Annotated[
        int,
        typer.Option(min=0, help="RNG seed, fixed so runs replay identical prefixes."),
    ] = 0,
    out_dir: Annotated[
        str, typer.Option(help="Directory for the per-cell result JSON.")
    ] = "bench/results",
    goodput: Annotated[
        list[str],
        typer.Option(
            callback=validate_goodput, help="SLO passed to the harness (repeatable)."
        ),
    ] = DEFAULT_GOODPUT,
    dry_run: Annotated[
        bool, typer.Option(help="Print the vllm commands instead of running them.")
    ] = False,
) -> None:
    """Sweep vllm bench serve across a prefix-share x burstiness grid."""
    try:
        config = SweepConfig(
            base_url=base_url,
            model=model,
            prefix_shares=prefix_share,
            burstiness_values=burstiness,
            total_len=total_len,
            num_prompts=num_prompts,
            num_prefixes=num_prefixes,
            output_len=output_len,
            align_blocks=align_blocks,
            request_rate=request_rate,
            seed=seed,
            out_dir=out_dir,
            goodput=goodput,
        )
        code = run_sweep(
            config,
            dry_run=dry_run,
            runner=run_cell,
            echo=typer.echo,
            warn=lambda line: typer.echo(line, err=True),
        )
    except SweepError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    raise typer.Exit(code=code)


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
        records = price_files([str(file) for file in files], inputs)
    except (CostError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(records, indent=2))


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
            metrics_before=str(metrics_before),
            metrics_after=str(metrics_after),
            result=str(result),
            cache_state=cache_state,
            model=model,
        )
    except (PrefixCacheError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(record, indent=2))


if __name__ == "__main__":
    app()
