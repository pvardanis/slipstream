"""Composition root for the bench executor: the ``slipstream-bench`` image CLI.

Assembles the cost sub-app and registers the single-cell executor commands the
bench-client image invokes beside it: ``load-cell`` (one ``vllm bench serve`` per
``docker run``, the grain the container executes; the grid loop belongs to the
orchestration layer, ADR-0012 §Amendment), ``load-sweep`` (the whole grid for a
laptop run), ``prefix-cache`` (the cold/warm hit-rate delta), and ``zero-leak`` (the
teardown money-safety check). Depends inward on the ``contract`` kernel alone.
"""

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer

from slipstream_bench.contract import ResultError, SweepError
from slipstream_bench.executor.config import load_cell_config, load_sweep_config
from slipstream_bench.executor.cost.cli import app as cost_app
from slipstream_bench.executor.prefix_cache import (
    CacheState,
    PrefixCacheError,
    scrape_prefix_cache,
)
from slipstream_bench.executor.runner import (
    CellOutcome,
    cell_command,
    ensure_out_dir,
    execute_cell,
    run_sweep,
)
from slipstream_bench.executor.zero_leak import LeakError, find_leaks, read_aws_json

app = typer.Typer(
    name="slipstream-bench",
    help="L0 bench executor: run a vllm bench serve cell, price it, scrape its cache.",
    no_args_is_help=True,
    add_completion=False,
)

# A nameless, callback-less sub-app merges its commands onto the root at the same
# level, so each concept groups its commands in its own module while the CLI keeps
# a flat command surface (`slipstream-bench cost`, not `... cost cost`).
app.add_typer(cost_app)


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

    A binary that cannot be executed — missing from PATH, not executable, a bad PATH
    component — is not a per-cell transient; every cell would fail the same way. So it
    fails fast with an actionable message rather than a raw traceback that would abort
    the grid before the tally. ``check=False`` means the child's own non-zero exit is
    returned, not raised, so the only exceptions here are exec-setup ``OSError``\\ s.

    :param command: the fully assembled cell command.
    :param extra_env: variables overlaid on the inherited environment for the
        child, e.g. a resolved API key; ``None`` inherits the parent unchanged.
    :return: the process exit code.
    :raise SweepError: when the command's binary cannot be executed.
    """
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        return subprocess.run(command, check=False, env=env).returncode
    except OSError as error:
        raise SweepError(
            f"cannot run '{command[0]}': {error.strerror or error} — the sweep runs "
            f"inside the baked bench-client image where vllm is installed"
        ) from error


def _resolve_extra_env(
    api_key_env: str | None, *, dry_run: bool
) -> dict[str, str] | None:
    """Resolve the child-process env overlay both load-sweep and load-cell share.

    A dry run builds no cells and touches no endpoint, so it does not need the key
    resolved — preview a commercial run without exporting a secret.

    :param api_key_env: env var holding the commercial key, or None for the
        self-hosted arm.
    :param dry_run: when true, skip resolving the key.
    :return: the child-process env overlay (the resolved key, or None for a
        self-hosted or dry run).
    :raise SweepError: on an unset key var on a live commercial run.
    """
    if api_key_env and not dry_run:
        return resolve_api_key_env(api_key_env)
    return None


@app.command("load-sweep")
def load_sweep(
    *,
    config: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="The experiment-definition YAML: the grid axes, lengths, SLO, seed.",
        ),
    ] = Path("bench/load-sweep.yaml"),
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible endpoint the sweep targets.")
    ] = "http://localhost:8000",
    model: Annotated[
        str,
        typer.Option(help="Served model id (model.yaml is its source of truth)."),
    ] = "Qwen/Qwen2.5-0.5B-Instruct",
    tokenizer: Annotated[
        str | None,
        typer.Option(
            envvar="SLIPSTREAM_BENCH_TOKENIZER_DIR",
            help="Local tokenizer for prompt synthesis, overriding any the config "
            "authors. The bench image sets this to its baked snapshot path so an "
            "offline run resolves the pinned tokenizer without a Hub call. Omit to "
            "let vLLM default it to --model.",
        ),
    ] = None,
    out_dir: Annotated[
        str, typer.Option(help="Directory for the per-cell result JSON.")
    ] = "bench/results",
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
    """Sweep vllm bench serve across the prefix-share x burstiness grid a config
    defines."""
    try:
        extra_env = _resolve_extra_env(api_key_env, dry_run=dry_run)
        cfg = load_sweep_config(
            config,
            base_url=base_url,
            model=model,
            out_dir=out_dir,
            commercial=api_key_env is not None,
            tokenizer=tokenizer,
        )
        code = run_sweep(
            cfg,
            dry_run=dry_run,
            runner=lambda command: run_cell(command, extra_env=extra_env),
            echo=typer.echo,
            warn=lambda line: typer.echo(line, err=True),
        )
    except SweepError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    raise typer.Exit(code=code)


@app.command("load-cell")
def load_cell(
    *,
    config: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="The cell-definition YAML: the coordinate (share, burstiness, "
            "optional max_concurrency) plus the shared lengths, SLO, seed.",
        ),
    ] = Path("bench/load-cell.yaml"),
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible endpoint the cell targets.")
    ] = "http://localhost:8000",
    model: Annotated[
        str,
        typer.Option(help="Served model id (model.yaml is its source of truth)."),
    ] = "Qwen/Qwen2.5-0.5B-Instruct",
    tokenizer: Annotated[
        str | None,
        typer.Option(
            envvar="SLIPSTREAM_BENCH_TOKENIZER_DIR",
            help="Local tokenizer for prompt synthesis, overriding any the config "
            "authors. The bench image sets this to its baked snapshot path so an "
            "offline run resolves the pinned tokenizer without a Hub call. Omit to "
            "let vLLM default it to --model.",
        ),
    ] = None,
    out_dir: Annotated[
        str, typer.Option(help="Directory for this cell's result JSON.")
    ] = "bench/results",
    api_key_env: Annotated[
        str | None,
        typer.Option(
            help="Env var holding the commercial API key, sent as OPENAI_API_KEY "
            "(kept off the command line). Omit for an unauthenticated endpoint."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option(help="Print the vllm command instead of running it.")
    ] = False,
) -> None:
    """Run one vllm bench serve cell — the coordinate its ``--config`` defines.

    The container executes one cell per ``docker run`` now that the per-cell loop
    lives in the orchestration layer (ADR-0012 §Amendment): the coordinate (share,
    burstiness, optional max_concurrency) and the shared knobs (lengths, SLO, seed)
    both come from ``--config``, and one result JSON is written. Exits 0 when the
    cell ran and was stamped, 1 when it failed to run or could not be stamped, 2 on
    any SweepError (an invalid config, an out-of-range coordinate, an unset key, an
    un-creatable out-dir, or block alignment erasing the prefix).
    """
    try:
        extra_env = _resolve_extra_env(api_key_env, dry_run=dry_run)
        cell = load_cell_config(
            config,
            base_url=base_url,
            model=model,
            out_dir=out_dir,
            commercial=api_key_env is not None,
            tokenizer=tokenizer,
        )
        if dry_run:
            typer.echo(" ".join(cell_command(cell)))
            raise typer.Exit(code=0)
        ensure_out_dir(cell.out_dir)
        outcome = execute_cell(
            cell,
            runner=lambda command: run_cell(command, extra_env=extra_env),
            echo=typer.echo,
            warn=lambda line: typer.echo(line, err=True),
        )
    except SweepError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    raise typer.Exit(code=0 if outcome is CellOutcome.OK else 1)


@app.command("prefix-cache")
def prefix_cache(
    *,
    cache_state: Annotated[
        CacheState,
        typer.Option(help="Which cache regime this run measured: cold or warm."),
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
            metrics_before=metrics_before,
            metrics_after=metrics_after,
            result=result,
            cache_state=cache_state,
            model=model,
        )
    except (PrefixCacheError, ResultError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(record, indent=2))


@app.command("zero-leak")
def zero_leak(
    *,
    instances: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="aws ec2 describe-instances JSON, tag-filtered to Project=slipstream.",
        ),
    ],
    volumes: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="aws ec2 describe-volumes JSON, tag-filtered to Project=slipstream.",
        ),
    ],
) -> None:
    """Assert cloud-verify teardown left no Project=slipstream resource still billing.

    Exit 0 when spend returned to zero, 1 when a tagged instance or volume
    survives (named on stderr), 2 when the AWS JSON is unreadable — a broken
    query fails loud rather than clearing a still-billing g5.
    """
    try:
        leaks = find_leaks(read_aws_json(instances), read_aws_json(volumes))
    except LeakError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    if leaks:
        for leak in leaks:
            typer.echo(f"leak: {leak.kind} {leak.identifier} ({leak.detail})", err=True)
        typer.echo(f"{len(leaks)} tagged leftover(s) still billing", err=True)
        raise typer.Exit(code=1)
    typer.echo("no tagged leftovers: GPU spend returned to zero")


if __name__ == "__main__":
    app()
