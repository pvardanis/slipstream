"""Front end for the L0 benchmark harness.

Dispatches the serve-sweep, cost, and prefix-cache benchmark subcommands.
"""

from typing import Annotated

import typer

from slipstream_bench.cli_helpers import (
    DEFAULT_BURSTINESS,
    DEFAULT_GOODPUT,
    DEFAULT_PREFIX_SHARES,
    not_implemented,
    run_cell,
    validate_goodput,
    validate_request_rate,
)
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
def cost() -> None:
    """Price a bench result into cost-per-1M input and output tokens."""
    not_implemented("cost")


@app.command("prefix-cache")
def prefix_cache() -> None:
    """Compute the cold/warm prefix-cache hit-rate delta for a bench run."""
    not_implemented("prefix-cache")


if __name__ == "__main__":
    app()
