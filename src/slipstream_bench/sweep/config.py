"""The knob set one sweep runs with, and the error a bad knob raises.

Split out from the runner so the config model and its validation live beside
the CLI options that populate it, with the runner importing both from here.
"""

from dataclasses import dataclass


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
