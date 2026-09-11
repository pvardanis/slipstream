"""Option defaults, input validators, and the cell runner the CLI wires in.

Keeps cli.py to command definitions only: the typer option defaults and
validators, the subprocess-backed cell runner, and the not-implemented
responder for the stub subcommands live here.
"""

import math
import subprocess

import typer

from slipstream_bench.serve_sweep import SweepError

# The sweep defaults, named so they read in --help. Immutable so one command's
# defaults can never leak into the next.
DEFAULT_PREFIX_SHARES = (10, 50, 90)
DEFAULT_BURSTINESS = (0.2, 1.0)
DEFAULT_GOODPUT = ("ttft:1000", "tpot:50")


def not_implemented(command: str) -> None:
    """Fail with a not-implemented notice on stderr and a non-zero exit.

    :param command: name of the subcommand whose body is still a stub.
    :raise typer.Exit: always, with exit code 1.
    """
    typer.echo(f"{command} is not implemented yet", err=True)
    raise typer.Exit(code=1)


def validate_request_rate(value: str) -> str:
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


def validate_goodput(values: list[str]) -> list[str]:
    """Reject an empty SLO so the sweep does not run without goodput tokens.

    :param values: the collected ``--goodput`` tokens (e.g. ``ttft:1000 tpot:50``).
    :return: the tokens unchanged when non-empty.
    :raise typer.BadParameter: when no non-empty token is present.
    """
    tokens = [token for token in values if token]
    if not tokens:
        raise typer.BadParameter("invalid --goodput: want e.g. 'ttft:1000 tpot:50'")
    return tokens


def run_cell(command: list[str]) -> int:
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
