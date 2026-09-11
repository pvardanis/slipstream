"""Front end for the L0 benchmark harness.

Dispatches the serve-sweep, cost, and prefix-cache benchmark subcommands.
"""

import typer

app = typer.Typer(
    name="slipstream-bench",
    help="L0 benchmark harness: serve sweeps, cost-per-1M, and prefix-cache hit rates.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("serve-sweep")
def serve_sweep() -> None:
    """Sweep vllm bench serve across a prefix-share x burstiness grid."""
    typer.echo("serve-sweep is not implemented yet")


@app.command("cost")
def cost() -> None:
    """Price a bench result into cost-per-1M input and output tokens."""
    typer.echo("cost is not implemented yet")


@app.command("prefix-cache")
def prefix_cache() -> None:
    """Compute the cold/warm prefix-cache hit-rate delta for a bench run."""
    typer.echo("prefix-cache is not implemented yet")


if __name__ == "__main__":
    app()
