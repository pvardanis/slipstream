"""Option defaults, input validators, and the cell runner the CLI wires in.

Keeps cli.py to command definitions only: the typer option defaults and
validators and the subprocess-backed cell runner live here.
"""

import math
import os
import subprocess
from collections.abc import Mapping

import typer

from slipstream_bench.serve_sweep import SweepError

# The sweep defaults, named so they read in --help. Immutable so one command's
# defaults can never leak into the next.
DEFAULT_PREFIX_SHARES = (10, 50, 90)
DEFAULT_BURSTINESS = (0.2, 1.0)
DEFAULT_GOODPUT = ("ttft:1000", "tpot:50")


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


def resolve_api_key_env(env_var: str) -> dict[str, str]:
    """Read a commercial API key from a named env var for the child process.

    The key is kept in the environment, never on the command line, so it survives
    the dry-run echo and process listings without leaking. It is handed to the
    child as ``OPENAI_API_KEY``, the variable the ``vllm bench serve`` OpenAI
    client backend (``--backend openai``) reads for the ``Authorization: Bearer``
    header.

    :param env_var: the environment variable holding the key.
    :return: the child-process env overlay setting ``OPENAI_API_KEY``.
    :raise SweepError: when the named variable is unset, empty, or whitespace-only,
        each of which would send a blank Bearer header on an unauthenticated run
        rather than fail.
    """
    value = os.environ.get(env_var)
    if not value or not value.strip():
        raise SweepError(
            f"--api-key-env {env_var}: environment variable '{env_var}' is unset "
            f"or empty — export the commercial API key before the sweep"
        )
    return {"OPENAI_API_KEY": value}


def run_cell(command: list[str], *, extra_env: Mapping[str, str] | None = None) -> int:
    """Run one cell's command, returning its exit code.

    A missing ``vllm`` binary is not a per-cell transient — every cell would fail
    the same way — so it fails fast with an actionable message rather than a raw
    traceback that would abort the grid before the tally.

    :param command: the fully assembled cell command.
    :param extra_env: variables overlaid on the inherited environment for the
        child, e.g. a resolved API key; ``None`` inherits the parent unchanged.
    :return: the process exit code.
    :raise SweepError: when the command's binary is not on PATH.
    """
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        return subprocess.run(command, check=False, env=env).returncode
    except FileNotFoundError as error:
        raise SweepError(
            f"cannot run '{command[0]}': not found on PATH — the sweep runs inside "
            f"the baked bench-client image where vllm is installed"
        ) from error
