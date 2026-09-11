"""Build and run one ``vllm bench serve`` per prefix-share x burstiness cell.

A thin wrapper over vLLM's native ``prefix_repetition`` workload, not a bespoke
load generator. It fires that workload across a grid of prefix-share % and
burstiness, applies the platform SLO as ``--goodput``, and saves the raw client
JSON (per-request TTFT/ITL via ``--save-detailed``) one file per grid cell.
Everything downstream — the cost-per-1M post-processor, the prefix-cache-hit
scraper — joins on that JSON.

Prefix-share % is the knob the routing sweep needs: it splits a fixed token
budget between the shared prefix and the per-request suffix, so share 90 means a
900/100 prefix/suffix split of a 1000-token budget. Where the sweep runs and
what ``--base-url`` it targets is orchestration, not tool logic.
"""

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
    """The knobs one sweep is run with; the grid is prefix_shares x burstiness_values."""

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

    def __post_init__(self) -> None:
        """Reject a config that would run zero cells or an out-of-range share.

        The CLI already rejects these with friendly messages; this guarantees the
        same for any other caller, so an empty grid can never report success
        having measured nothing.

        :raise SweepError: on an empty grid axis, an empty SLO, a share outside
            0..100, or fewer prompts than prefixes.
        """
        if not self.prefix_shares:
            raise SweepError("no prefix-shares to sweep: the grid would be empty")
        if not self.burstiness_values:
            raise SweepError("no burstiness values to sweep: the grid would be empty")
        if not self.goodput:
            raise SweepError("no goodput SLO given: want e.g. 'ttft:1000 tpot:50'")
        for share in self.prefix_shares:
            if not 0 <= share <= 100:
                raise SweepError(f"prefix-share {share} is outside 0..100")
        # vLLM's prefix_repetition workload gives each prefix at least one prompt,
        # so it rejects a run with more prefixes than prompts. Fail fast here rather
        # than let every cell die the same way mid-grid.
        if self.num_prompts < self.num_prefixes:
            raise SweepError(
                f"--num-prompts {self.num_prompts} is below --num-prefixes "
                f"{self.num_prefixes}: raise prompts or lower prefixes"
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


def grid(config: SweepConfig) -> Iterator[tuple[int, float]]:
    """Yield every (prefix-share, burstiness) cell, shares outermost."""
    yield from product(config.prefix_shares, config.burstiness_values)


def _result_file(config: SweepConfig, share: int, burstiness: float) -> str:
    """Name a distinct result JSON for one grid cell."""
    return f"{config.out_dir}/pshare{share}_burst{burstiness}.json"


def cell_command(config: SweepConfig, *, share: int, burstiness: float) -> list[str]:
    """Assemble the ``vllm bench serve`` command for one grid cell.

    ``--goodput`` carries the SLO; ``--save-result``/``--save-detailed`` writes the
    raw per-request client JSON; ``--percentile-metrics`` + ``--metric-percentiles``
    report p95 and p99 side by side for ttft, tpot, itl, and e2el.

    :param config: the sweep knobs shared across every cell.
    :param share: this cell's prefix-share percentage.
    :param burstiness: this cell's burstiness (low = bursty, 1.0 = Poisson).
    :return: the argv list for this cell.
    :raise SweepError: when block alignment would erase the prefix (see
        :func:`split_lengths`).
    """
    prefix_len, suffix_len = split_lengths(
        config.total_len, share, align_blocks=config.align_blocks
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
        "--goodput",
        *config.goodput,
        "--percentile-metrics",
        "ttft,tpot,itl,e2el",
        "--metric-percentiles",
        "95,99",
        "--save-result",
        "--save-detailed",
        "--result-filename",
        _result_file(config, share, burstiness),
    ]


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
    :return: 0 when every cell succeeded (or dry run), 1 when any cell failed.
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
    for share, burstiness in grid(config):
        command = cell_command(config, share=share, burstiness=burstiness)
        if dry_run:
            echo(" ".join(command))
            continue
        result_file = _result_file(config, share, burstiness)
        echo(f"==> prefix-share {share}% burstiness {burstiness} -> {result_file}")
        code = runner(command)
        if code == 0:
            completed += 1
        else:
            failed += 1
            warn(
                f"!! cell prefix-share {share}% burstiness {burstiness} "
                f"failed (exit {code})"
            )

    if failed > 0:
        warn(f"sweep finished: {completed} cells ok, {failed} failed")
        return 1
    return 0
