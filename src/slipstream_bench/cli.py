"""Front end for the L0 benchmark harness.

Dispatches the serve-sweep, cost, and prefix-cache benchmark subcommands.
"""

import math
import subprocess
from typing import Annotated

import typer

from slipstream_bench.serve_sweep import SweepConfig, SweepError, run_sweep

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


# The sweep defaults, named so they read in --help. Immutable so one command's
# defaults can never leak into the next.
_DEFAULT_PREFIX_SHARES = (10, 50, 90)
_DEFAULT_BURSTINESS = (0.2, 1.0)
_DEFAULT_GOODPUT = ("ttft:1000", "tpot:50")


def _validate_request_rate(value: str) -> str:
    """Reject a request rate that is neither a non-negative number nor 'inf'.

    :param value: the raw ``--request-rate`` argument.
    :return: the value unchanged when valid.
    :raise typer.BadParameter: when it is negative, or neither a number nor 'inf'.
    """
    if value == "inf":
        return value
    try:
        rate = float(value)
    except ValueError:
        raise typer.BadParameter(
            f"invalid --request-rate '{value}': want a number or 'inf'"
        ) from None
    if not math.isfinite(rate) or rate < 0:
        raise typer.BadParameter(
            f"invalid --request-rate '{value}': want a non-negative number or 'inf'"
        )
    return value


def _validate_goodput(values: list[str]) -> list[str]:
    """Reject an empty SLO so the sweep does not run without goodput tokens.

    :param values: the collected ``--goodput`` tokens (e.g. ``ttft:1000 tpot:50``).
    :return: the tokens unchanged when non-empty.
    :raise typer.BadParameter: when no non-empty token is present.
    """
    tokens = [token for token in values if token]
    if not tokens:
        raise typer.BadParameter("invalid --goodput: want e.g. 'ttft:1000 tpot:50'")
    return tokens


def _run_cell(command: list[str]) -> int:
    """Run one cell's command, returning its exit code.

    A missing ``vllm`` binary is not a per-cell transient — every cell would fail
    the same way — so it fails fast with an actionable message rather than a raw
    traceback that would abort the grid before the tally.

    :param command: the fully assembled cell command.
    :return: the process exit code.
    :raise SweepError: when the command's binary is not on PATH.
    """
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError as error:
        raise SweepError(
            f"cannot run '{command[0]}': not found on PATH — the sweep runs inside "
            f"the baked bench-client image where vllm is installed"
        ) from error


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
    ] = _DEFAULT_PREFIX_SHARES,
    burstiness: Annotated[
        list[float],
        typer.Option(
            min=0, help="Burstiness to sweep, low = bursty, 1.0 = Poisson (repeatable)."
        ),
    ] = _DEFAULT_BURSTINESS,
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
        typer.Option(callback=_validate_request_rate, help="Requests/sec, or 'inf'."),
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
            callback=_validate_goodput, help="SLO passed to the harness (repeatable)."
        ),
    ] = _DEFAULT_GOODPUT,
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
            runner=_run_cell,
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
    _not_implemented("cost")


@app.command("prefix-cache")
def prefix_cache() -> None:
    """Compute the cold/warm prefix-cache hit-rate delta for a bench run."""
    _not_implemented("prefix-cache")


if __name__ == "__main__":
    app()
