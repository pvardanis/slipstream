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


def _not_implemented(command: str) -> None:
    """Fail with a not-implemented notice on stderr and a non-zero exit.

    :param command: name of the subcommand whose body is still a stub.
    :raise typer.Exit: always, with exit code 1.
    """
    typer.echo(f"{command} is not implemented yet", err=True)
    raise typer.Exit(code=1)


@app.command("serve-sweep")
def serve_sweep() -> None:
    """Sweep vllm bench serve across a prefix-share x burstiness grid."""
    _not_implemented("serve-sweep")


@app.command("cost")
def cost() -> None:
    """Price a bench result into cost-per-1M input and output tokens."""
    _not_implemented("cost")


@app.command("prefix-cache")
def prefix_cache() -> None:
    """Compute the cold/warm prefix-cache hit-rate delta for a bench run."""
    _not_implemented("prefix-cache")


if __name__ == "__main__":
    app()
