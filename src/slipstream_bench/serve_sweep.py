"""Build and run one ``vllm bench serve`` per prefix-share x burstiness x cap cell.

A thin wrapper over vLLM's native ``prefix_repetition`` workload, not a bespoke
load generator. It fires that workload across a grid of prefix-share %,
burstiness, and an optional closed-loop max-concurrency ladder, applies the
platform SLO as ``--goodput``, and saves the raw client JSON (per-request
TTFT/ITL via ``--save-detailed``) one file per grid cell. Everything downstream —
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import product
from pathlib import Path

# A cell runner takes a fully assembled command and returns its process exit code.
CellRunner = Callable[[list[str]], int]

# Where the built command line and per-cell progress lines are emitted.
Echo = Callable[[str], None]


class SweepError(Exception):
    """A sweep input that cannot produce a meaningful measurement."""


@dataclass(frozen=True)
class SweepConfig:
    """The knobs one sweep is run with.

    The grid is prefix_shares x burstiness_values x max_concurrency_values. An empty
    ``max_concurrency_values`` sweeps open-loop (arrival-rate bound by request_rate,
    no in-flight cap); a non-empty ladder sweeps closed-loop, capping in-flight
    requests with ``vllm bench serve --max-concurrency`` — the axis the concurrency
    ceiling is read off (ADR-0009).
    """

    base_url: str
    model: str
    prefix_shares: list[int]
    burstiness_values: list[float]
    total_len: int
    num_prompts: int
    num_prefixes: int
    output_len: int
    align_blocks: int
    request_rate: str
    seed: int
    out_dir: str
    goodput: list[str]
    tokenizer: str | None = None
    commercial: bool = False
    max_concurrency_values: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        """Reject a config that would run zero cells or an out-of-range share.

        The CLI already rejects these with friendly messages; this guarantees the
        same for any other caller, so an empty grid can never report success
        having measured nothing.

        :raise SweepError: on an empty grid axis, an empty SLO, a share outside
            0..100, a max-concurrency cap below 1, a repeated grid-axis value,
            fewer prompts than prefixes, or a commercial run without a local
            tokenizer.
        """
        if not self.prefix_shares:
            raise SweepError("no prefix-shares to sweep: the grid would be empty")
        if not self.burstiness_values:
            raise SweepError("no burstiness values to sweep: the grid would be empty")
        if not self.goodput:
            raise SweepError("no goodput SLO given: want e.g. 'ttft:1000 tpot:50'")
        self._reject_out_of_range_shares()
        self._reject_nonpositive_caps()
        self._reject_duplicate_axis_values()
        # vLLM's prefix_repetition workload gives each prefix at least one prompt,
        # so it rejects a run with more prefixes than prompts. Fail fast here rather
        # than let every cell die the same way mid-grid.
        if self.num_prompts < self.num_prefixes:
            raise SweepError(
                f"--num-prompts {self.num_prompts} is below --num-prefixes "
                f"{self.num_prefixes}: raise prompts or lower prefixes"
            )
        self._require_tokenizer_when_commercial()

    def _reject_out_of_range_shares(self) -> None:
        """Reject a prefix-share outside 0..100, which names no valid split.

        :raise SweepError: on a prefix-share below 0 or above 100.
        """
        for share in self.prefix_shares:
            if not 0 <= share <= 100:
                raise SweepError(f"prefix-share {share} is outside 0..100")

    def _reject_nonpositive_caps(self) -> None:
        """Reject a closed-loop max-concurrency cap that admits no in-flight request.

        A cap of zero (or below) lets no request run, so every cell at that rung
        would measure nothing. Fail fast before the grid runs.

        :raise SweepError: on a max-concurrency cap below 1.
        """
        for cap in self.max_concurrency_values:
            if cap < 1:
                raise SweepError(f"--max-concurrency {cap} is below 1")

    def _reject_duplicate_axis_values(self) -> None:
        """Reject a grid axis that repeats a value, which reruns an identical cell.

        The grid is the cartesian product of the three axes, so a repeated
        prefix-share, burstiness, or max-concurrency value runs the same cell
        twice — a full ``vllm bench serve`` pass whose result file, named from
        the cell's coordinates, overwrites the first cell's own output. Fail fast
        before the grid wastes the run.

        :raise SweepError: on a repeated value in prefix_shares, burstiness_values,
            or max_concurrency_values.
        """
        axes = (
            ("prefix-share", self.prefix_shares),
            ("burstiness", self.burstiness_values),
            ("max-concurrency", self.max_concurrency_values),
        )
        for label, values in axes:
            seen: set[object] = set()
            for value in values:
                if value in seen:
                    raise SweepError(
                        f"duplicate {label} {value}: each grid value sweeps once"
                    )
                seen.add(value)

    def _require_tokenizer_when_commercial(self) -> None:
        """Reject a commercial sweep that has no local tokenizer for synthesis.

        A commercial ``--model`` is a provider id (e.g. gpt-4o-mini) vLLM cannot
        load as an HF tokenizer, so prompt synthesis needs an explicit local one.
        The provider bills on its own tokenizer regardless; this only fixes the
        workload text, and every cell would die the same way without it.

        :raise SweepError: on a commercial run with no ``tokenizer`` set.
        """
        if self.commercial and not (self.tokenizer and self.tokenizer.strip()):
            raise SweepError(
                "a commercial sweep (--api-key-env) needs --tokenizer: the provider "
                "--model will not resolve as a local tokenizer for prompt synthesis"
            )


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
                f"--align-blocks {align_blocks} floors prefix {prefix_len} "
                f"(share {share}% of {total_len}) to 0 — raise --total-len or "
                f"--prefix-share, or lower --align-blocks"
            )
        prefix_len = aligned
    return prefix_len, total_len - prefix_len


def grid(config: SweepConfig) -> Iterator[tuple[int, float, int | None]]:
    """Yield every (prefix-share, burstiness, max-concurrency) cell, shares outermost.

    The max-concurrency ladder is the innermost axis, so every rung of it runs
    contiguously within one (share, burstiness) pair — the sequence the concurrency
    ceiling is read off. With no ladder configured the axis is a single open-loop
    ``None``, one cell per (share, burstiness) pair.
    """
    # An empty ladder is open-loop: fall back to a single ``None`` rung so the grid
    # still yields one cell per (share, burstiness) pair. The Cartesian product then
    # walks shares outermost and the ladder innermost.
    ladder: tuple[int | None, ...] = config.max_concurrency_values or (None,)
    yield from product(config.prefix_shares, config.burstiness_values, ladder)


def _result_file(
    config: SweepConfig, share: int, burstiness: float, max_concurrency: int | None
) -> str:
    """Name a distinct result JSON for one grid cell.

    A closed-loop cell carries its cap in the name so ladder rungs never collide;
    an open-loop cell (no cap) is left un-suffixed.
    """
    cap = f"_mc{max_concurrency}" if max_concurrency is not None else ""
    return f"{config.out_dir}/pshare{share}_burst{burstiness}{cap}.json"


def cell_command(
    config: SweepConfig,
    *,
    share: int,
    burstiness: float,
    max_concurrency: int | None = None,
) -> list[str]:
    """Assemble the ``vllm bench serve`` command for one grid cell.

    ``--goodput`` carries the SLO; ``--save-result``/``--save-detailed`` writes the
    raw per-request client JSON; ``--percentile-metrics`` + ``--metric-percentiles``
    report p95 and p99 side by side for ttft, tpot, itl, and e2el. A set
    ``max_concurrency`` caps in-flight requests (closed-loop); omitting it leaves the
    arrival rate the sole limiter (open-loop).

    :param config: the sweep knobs shared across every cell.
    :param share: this cell's prefix-share percentage.
    :param burstiness: this cell's burstiness (low = bursty, 1.0 = Poisson).
    :param max_concurrency: this cell's in-flight cap, or None for open-loop.
    :return: the argv list for this cell.
    :raise SweepError: when block alignment would erase the prefix (see
        :func:`split_lengths`).
    """
    prefix_len, suffix_len = split_lengths(
        config.total_len, share, align_blocks=config.align_blocks
    )
    # A local tokenizer for prompt synthesis; vLLM defaults it to --model when
    # omitted, which only works for the self-hosted arm's HF model id. The
    # commercial arm's billed token counts come from the provider's usage block
    # when the provider returns one, which vLLM v0.29.0 always requests
    # (stream_options include_usage), so no flag forces it here; on a missing
    # usage block vLLM silently retokenizes locally (see commercial_cost).
    tokenizer_args = (
        ["--tokenizer", config.tokenizer]
        if config.tokenizer and config.tokenizer.strip()
        else []
    )
    # A closed-loop cell caps in-flight requests with the client-side semaphore;
    # an open-loop cell omits the flag and lets the arrival rate alone limit load.
    concurrency_args = (
        ["--max-concurrency", str(max_concurrency)]
        if max_concurrency is not None
        else []
    )
    return [
        "vllm",
        "bench",
        "serve",
        "--backend",
        "openai",
        "--base-url",
        config.base_url,
        "--model",
        config.model,
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
        str(config.num_prefixes),
        "--prefix-repetition-output-len",
        str(config.output_len),
        "--num-prompts",
        str(config.num_prompts),
        "--request-rate",
        config.request_rate,
        "--seed",
        str(config.seed),
        "--burstiness",
        str(burstiness),
        *concurrency_args,
        "--goodput",
        *config.goodput,
        "--percentile-metrics",
        "ttft,tpot,itl,e2el",
        "--metric-percentiles",
        "95,99",
        "--save-result",
        "--save-detailed",
        "--result-filename",
        _result_file(config, share, burstiness, max_concurrency),
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


def _cell_label(share: int, burstiness: float, max_concurrency: int | None) -> str:
    """Describe one cell for its progress and failure lines.

    A closed-loop cell names its in-flight cap so its lines are told apart from the
    open-loop pass; an open-loop cell (no cap) carries none.
    """
    cap = "" if max_concurrency is None else f" max-concurrency {max_concurrency}"
    return f"prefix-share {share}% burstiness {burstiness}{cap}"


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
        try:
            Path(config.out_dir).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SweepError(
                f"cannot create out-dir '{config.out_dir}': {error.strerror}"
            ) from error

    completed = 0
    failed = 0
    unannotated = 0
    for share, burstiness, max_concurrency in grid(config):
        command = cell_command(
            config, share=share, burstiness=burstiness, max_concurrency=max_concurrency
        )
        if dry_run:
            echo(" ".join(command))
            continue
        result_file = _result_file(config, share, burstiness, max_concurrency)
        label = _cell_label(share, burstiness, max_concurrency)
        echo(f"==> {label} -> {result_file}")
        code = runner(command)
        if code != 0:
            failed += 1
            warn(f"!! cell {label} failed (exit {code})")
            continue
        completed += 1
        # A cell that ran but cannot be stamped yields a result the report will
        # reject, so it is not a clean success — tally it and fail the sweep.
        if not _annotate_prefix_share(result_file, share, warn):
            unannotated += 1

    if failed > 0 or unannotated > 0:
        warn(
            f"sweep finished: {completed} cells ok, {failed} failed, "
            f"{unannotated} un-annotated"
        )
        return 1
    return 0
