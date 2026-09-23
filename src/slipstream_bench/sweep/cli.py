"""The sweep concept's Typer sub-app: run, aggregate, and emit the grid.

Owns the ``serve-sweep``, ``aggregate-sweep``, and ``sweep-grid`` commands, plus
the option defaults, input validators, and cell runner that guard ``serve-sweep``.
"""

import json
import math
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.results import ResultError
from slipstream_bench.sweep.aggregation import (
    SweepAggregationError,
    aggregate_ceilings,
)
from slipstream_bench.sweep.config import SweepConfig, SweepError
from slipstream_bench.sweep.grid import (
    SweepGridError,
    SweepGridPart,
    load_grid,
    render_part,
)
from slipstream_bench.sweep.runner import run_sweep

app = typer.Typer()

# The sweep defaults, named so they read in --help. Immutable so one command's
# defaults can never leak into the next.
DEFAULT_PREFIX_SHARES = (10, 50, 90)
DEFAULT_BURSTINESS = (0.2, 1.0)
DEFAULT_GOODPUT = ("ttft:1000", "tpot:50")
# Empty by default: the sweep is open-loop unless a closed-loop ladder is passed.
DEFAULT_MAX_CONCURRENCY: tuple[int, ...] = ()


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


@app.command("serve-sweep")
def serve_sweep(
    *,
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible endpoint the sweep targets.")
    ] = "http://localhost:8000",
    model: Annotated[str, typer.Option(help="Served model id.")] = (
        "Qwen/Qwen2.5-0.5B-Instruct"
    ),
    tokenizer: Annotated[
        str | None,
        typer.Option(
            help="Local tokenizer for prompt synthesis (defaults to --model). "
            "Required with --api-key-env: a provider --model will not resolve."
        ),
    ] = None,
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
    max_concurrency: Annotated[
        list[int],
        typer.Option(
            min=1,
            help="In-flight request cap to ladder, closed-loop (repeatable). "
            "Omit to sweep open-loop, bound only by --request-rate.",
        ),
    ] = DEFAULT_MAX_CONCURRENCY,
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
    api_key_env: Annotated[
        str | None,
        typer.Option(
            help="Env var holding the commercial API key, sent as OPENAI_API_KEY "
            "(kept off the command line). Omit for an unauthenticated endpoint."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option(help="Print the vllm commands instead of running them.")
    ] = False,
) -> None:
    """Sweep vllm bench serve across a prefix-share x burstiness grid."""
    try:
        # A dry run builds no cells and touches no endpoint, so it does not need
        # the key resolved — preview a commercial sweep without exporting a secret.
        extra_env = (
            resolve_api_key_env(api_key_env) if api_key_env and not dry_run else None
        )
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
            tokenizer=tokenizer,
            commercial=api_key_env is not None,
            max_concurrency_values=tuple(max_concurrency),
        )
        code = run_sweep(
            config,
            dry_run=dry_run,
            runner=lambda command: run_cell(command, extra_env=extra_env),
            echo=typer.echo,
            warn=lambda line: typer.echo(line, err=True),
        )
    except SweepError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    raise typer.Exit(code=code)


@app.command("aggregate-sweep")
def aggregate_sweep(
    *,
    run_dir: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="The bench/results/<run_id> directory the sweep wrote, one subdir "
            "per engine-knob point.",
        ),
    ],
) -> None:
    """Aggregate a knob-sweep run's saved results into the concurrency-ceiling table.

    Emits one JSON row per (max-num-seqs, kv-cache-dtype, prefix-caching) point and
    prefix-share: the measured ceiling and the {timeout, oom, other} failure
    cohorts. oom and num_preemptions read null — the recipe does not collect the pod
    events and /metrics snapshots they need (ADR-0009).
    """
    try:
        rows = aggregate_ceilings(run_dir)
    except (SweepAggregationError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(rows, indent=2))


@app.command("sweep-grid")
def sweep_grid(
    part: Annotated[
        SweepGridPart,
        typer.Argument(help="Which grid values to emit for the knob-sweep loop."),
    ],
    *,
    grid: Annotated[
        Path, typer.Option(help="The sweep grid YAML the recipe varies its knobs over.")
    ] = Path("bench/sweep-grid.yaml"),
) -> None:
    """Emit a validated slice of the knob-sweep grid for `just knob-sweep` to read.

    `points` prints the Tier-1 engine points as TSV (one redeploy per row, keyed by
    its results-subdir slug), `ladder` the Tier-2 --max-concurrency rungs, and
    `burstiness` the pinned scalar. The whole grid is validated first, so an invalid
    value fails here rather than mid-sweep on a live GPU (ADR-0009).
    """
    try:
        loaded = load_grid(grid)
    except SweepGridError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(render_part(part, loaded))
