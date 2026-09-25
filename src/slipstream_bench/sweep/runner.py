"""Build and run one ``vllm bench serve`` per prefix-share x burstiness x cap cell.

A thin wrapper over vLLM's native ``prefix_repetition`` workload, not a bespoke
load generator. It fires that workload for one cell — a prefix-share %, a
burstiness, and an optional closed-loop max-concurrency cap — applies the
platform SLO as ``--goodput``, and saves the raw client JSON (per-request
TTFT/ITL via ``--save-detailed``) one file per cell. Everything downstream —
the cost-per-1M post-processor, the prefix-cache-hit scraper, the concurrency
ceiling — joins on that JSON.

Prefix-share % is the knob the routing sweep needs: it splits a fixed token
budget between the shared prefix and the per-request suffix, so share 90 means a
900/100 prefix/suffix split of a 1000-token budget. vLLM records the split
lengths but not the share, so each successful cell's share is stamped onto its
result JSON, the key the baseline report segments on. Where the sweep runs and
what ``--base-url`` it targets is orchestration, not tool logic.
"""

import json
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from slipstream_bench.sweep.config import CellConfig, SweepConfig, SweepError

# A cell runner takes a fully assembled command and returns its process exit code.
CellRunner = Callable[[list[str]], int]

# Where the built command line and per-cell progress lines are emitted.
Echo = Callable[[str], None]


class CellOutcome(Enum):
    """How one cell's run ended, so the caller tallies and exits on it.

    A cell is a clean success only when it ran and was stamped. Both a non-zero
    exit and a stamp that could not be written are failures — the second because a
    result the report will later reject is not a measurement — so both fail the
    caller, but they are counted apart: a failed cell never ran to completion, an
    un-annotated one did but is unusable.
    """

    OK = "ok"
    FAILED = "failed"
    UNANNOTATED = "unannotated"


def split_lengths(total_len: int, share: int, *, align_blocks: int) -> tuple[int, int]:
    """Split a token budget into prefix and suffix lengths for one prefix-share.

    The prefix is floored to whole tokens (share 33 of 100 is a 33/67 split), and
    when ``align_blocks`` is set it is floored further to a multiple of that block
    size. vLLM's prefix cache hashes the prompt in fixed-size blocks and reuses
    whole blocks only, so a ragged tail recomputes every time and dilutes the
    cold/warm gap. Flooring (never rounding up) keeps the prefix within the budget,
    so the suffix stays non-negative.

    :param total_len: prefix + suffix token budget.
    :param share: prefix-share percentage in 0..100.
    :param align_blocks: block size to floor the prefix to; 0 disables alignment.
    :return: the (prefix_len, suffix_len) pair, summing to ``total_len``.
    :raise SweepError: when alignment floors a non-empty prefix to 0, which would
        erase the shared prefix the run exists to measure.
    """
    prefix_len = total_len * share // 100
    # A share of 0 asks for no prefix, which floors to 0 legitimately; a non-empty
    # prefix flooring to 0 would erase the shared prefix, so that fails fast.
    if align_blocks > 0 and prefix_len > 0:
        aligned = prefix_len // align_blocks * align_blocks
        if aligned == 0:
            raise SweepError(
                f"align_blocks {align_blocks} floors prefix {prefix_len} "
                f"(share {share}% of {total_len}) to 0 — raise total_len or the "
                f"prefix_shares entry, or lower align_blocks"
            )
        prefix_len = aligned
    return prefix_len, total_len - prefix_len


def _result_file(cell: CellConfig) -> str:
    """Name a distinct result JSON for one cell.

    A closed-loop cell carries its cap in the name so ladder rungs never collide;
    an open-loop cell (no cap) is left un-suffixed.
    """
    cap = f"_mc{cell.max_concurrency}" if cell.max_concurrency is not None else ""
    return f"{cell.out_dir}/pshare{cell.share}_burst{cell.burstiness}{cap}.json"


def cell_command(cell: CellConfig) -> list[str]:
    """Assemble the ``vllm bench serve`` command for one cell.

    ``--goodput`` carries the SLO; ``--save-result``/``--save-detailed`` writes the
    raw per-request client JSON; ``--percentile-metrics`` + ``--metric-percentiles``
    report p95 and p99 side by side for ttft, tpot, itl, and e2el. A set
    ``max_concurrency`` caps in-flight requests (closed-loop); omitting it leaves the
    arrival rate the sole limiter (open-loop).

    :param cell: the cell's knobs and coordinate.
    :return: the argv list for this cell.
    :raise SweepError: when block alignment would erase the prefix (see
        :func:`split_lengths`).
    """
    prefix_len, suffix_len = split_lengths(
        cell.total_len, cell.share, align_blocks=cell.align_blocks
    )
    # A local tokenizer for prompt synthesis; vLLM defaults it to --model when
    # omitted, which only works for the self-hosted arm's HF model id. The
    # commercial arm's billed token counts come from the provider's usage block
    # when the provider returns one, which vLLM v0.29.0 always requests
    # (stream_options include_usage), so no flag forces it here; on a missing
    # usage block vLLM silently retokenizes locally (see cost.commercial).
    tokenizer_args = (
        ["--tokenizer", cell.tokenizer]
        if cell.tokenizer and cell.tokenizer.strip()
        else []
    )
    # A closed-loop cell caps in-flight requests with the client-side semaphore;
    # an open-loop cell omits the flag and lets the arrival rate alone limit load.
    concurrency_args = (
        ["--max-concurrency", str(cell.max_concurrency)]
        if cell.max_concurrency is not None
        else []
    )
    return [
        "vllm",
        "bench",
        "serve",
        "--backend",
        "openai",
        "--base-url",
        cell.base_url,
        "--model",
        cell.model,
        *tokenizer_args,
        "--endpoint",
        "/v1/completions",
        "--dataset-name",
        "prefix_repetition",
        "--prefix-repetition-prefix-len",
        str(prefix_len),
        "--prefix-repetition-suffix-len",
        str(suffix_len),
        "--prefix-repetition-num-prefixes",
        str(cell.num_prefixes),
        "--prefix-repetition-output-len",
        str(cell.output_len),
        "--num-prompts",
        str(cell.num_prompts),
        "--request-rate",
        cell.request_rate,
        "--seed",
        str(cell.seed),
        "--burstiness",
        str(cell.burstiness),
        *concurrency_args,
        "--goodput",
        *cell.goodput,
        "--percentile-metrics",
        "ttft,tpot,itl,e2el",
        "--metric-percentiles",
        "95,99",
        "--save-result",
        "--save-detailed",
        "--result-filename",
        _result_file(cell),
    ]


def _annotate_prefix_share(result_file: str, share: int, warn: Echo) -> bool:
    """Inject a cell's prefix-share into the result JSON vLLM wrote.

    vLLM's ``--save-result`` records the run's request_rate but not the
    prefix-share (it takes prefix/suffix lengths, not a share), so the baseline
    report has no way to segment by share unless the sweep stamps it on. A cell
    that ran but cannot be stamped produces a result the report will later reject,
    so the stamp failing is reported back to the tally rather than swallowed —
    the cell is not a clean success.

    :param result_file: the cell's ``--result-filename`` path.
    :param share: this cell's prefix-share percentage to stamp on.
    :param warn: sink for the failure line.
    :return: True when the share was stamped, False when it could not be.
    """
    path = Path(result_file)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        warn(f"!! could not read {result_file} to inject prefix-share: {error}")
        return False
    if not isinstance(record, dict):
        warn(f"!! {result_file} is not a JSON object; prefix-share not injected")
        return False
    record["prefix_share"] = share
    try:
        path.write_text(json.dumps(record), encoding="utf-8")
    except OSError as error:
        warn(f"!! could not write prefix-share into {result_file}: {error}")
        return False
    return True


def _cell_label(cell: CellConfig) -> str:
    """Describe one cell for its progress and failure lines.

    A closed-loop cell names its in-flight cap so its lines are told apart from the
    open-loop pass; an open-loop cell (no cap) carries none.
    """
    cap = (
        ""
        if cell.max_concurrency is None
        else f" max-concurrency {cell.max_concurrency}"
    )
    return f"prefix-share {cell.share}% burstiness {cell.burstiness}{cap}"


def ensure_out_dir(out_dir: str) -> None:
    """Create the result directory, including missing parents.

    Both the grid sweep and the single-cell executor call this before running any
    cell, so the same missing-directory failure reads the same way whichever drives
    the run.

    :param out_dir: the directory the cells' result JSON is written into.
    :raise SweepError: when the directory cannot be created.
    """
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SweepError(f"cannot create out-dir '{out_dir}': {error}") from error


def execute_cell(
    cell: CellConfig,
    *,
    runner: CellRunner,
    echo: Echo,
    warn: Echo,
) -> CellOutcome:
    """Run one cell: build its command, run it, stamp its prefix-share.

    The unit of resume is the cell (ADR-0012 §Amendment): the grid loop lives in
    the orchestration layer and hands each cell here, one ``vllm bench serve`` per
    call. ``run_sweep`` drives the whole grid through this same body, so the two
    paths run a cell identically.

    :param cell: the cell's knobs and coordinate.
    :param runner: runs the cell's command and returns its process exit code.
    :param echo: sink for the per-cell progress line.
    :param warn: sink for the failure and stamp-failure lines.
    :return: OK when the cell ran and was stamped, FAILED on a non-zero exit,
        UNANNOTATED when it ran but its prefix-share could not be stamped.
    :raise SweepError: when block alignment would erase the prefix (see
        :func:`split_lengths`).
    """
    command = cell_command(cell)
    result_file = _result_file(cell)
    label = _cell_label(cell)
    echo(f"==> {label} -> {result_file}")
    code = runner(command)
    if code != 0:
        warn(f"!! cell {label} failed (exit {code})")
        return CellOutcome.FAILED
    # A cell that ran but cannot be stamped yields a result the report will reject,
    # so it is not a clean success.
    if not _annotate_prefix_share(result_file, cell.share, warn):
        return CellOutcome.UNANNOTATED
    return CellOutcome.OK


def run_sweep(
    config: SweepConfig,
    *,
    dry_run: bool,
    runner: CellRunner,
    echo: Echo,
    warn: Echo,
) -> int:
    """Run the grid, one ``vllm bench serve`` per cell, and report the tally.

    A cell failing (a transient vLLM error, say) does not discard the cells still
    to run: the grid finishes, then the tally is reported and the exit code is
    non-zero if any cell failed. ``--dry-run`` prints each command and runs none.

    :param config: the sweep knobs and grid.
    :param dry_run: when true, echo each command instead of running it.
    :param runner: runs one cell's command and returns its process exit code.
    :param echo: sink for the dry-run commands and the per-cell progress lines.
    :param warn: sink for the failure and tally lines (stderr, so they survive a
        stdout redirect meant for the commands or results).
    :return: 0 when every cell ran and was stamped (or dry run), 1 when any cell
        failed to run or could not be stamped with its prefix-share.
    :raise SweepError: when block alignment would erase a prefix (see
        :func:`split_lengths`), or the out-dir cannot be created.
    """
    if not dry_run:
        ensure_out_dir(config.out_dir)

    completed = 0
    failed = 0
    unannotated = 0
    for cell in config.cells():
        if dry_run:
            echo(" ".join(cell_command(cell)))
            continue
        outcome = execute_cell(cell, runner=runner, echo=echo, warn=warn)
        if outcome is CellOutcome.FAILED:
            failed += 1
            continue
        # A cell that ran counts completed even when its stamp failed; an
        # un-annotated one is tallied on top so the sweep still fails.
        completed += 1
        if outcome is CellOutcome.UNANNOTATED:
            unannotated += 1

    if failed > 0 or unannotated > 0:
        warn(
            f"sweep finished: {completed} cells ok, {failed} failed, "
            f"{unannotated} un-annotated"
        )
        return 1
    return 0
