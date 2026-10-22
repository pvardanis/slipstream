"""Fold a kv-cache-extension-ab run's labelled records into per-arm rung spreads and ceilings.

The KV cache offload A/B (#326) labels each rung of each arm run, and its render joins the
labels to the rung's raw ``vllm bench serve`` result into one record: the raw result plus a
``kv_ab`` block naming its arm and run and its prefix-cache hit rates
(:mod:`slipstream_bench.contract.kv_ab_record`). A record reads into
a :class:`KvAbRung`, the raw result parsed as a load cell beside its labels. The fold
summarises each arm's rung across its runs as the median and range of its metrics
(:func:`summarize_kv_ab_rungs`), the runs too few for a confidence interval, and reads each
arm run's concurrency ceiling off its own ladder (:func:`get_kv_ab_ceilings`) the way the
knob sweep reads a point's (ADR-0009).
"""

import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast, get_args

from slipstream_bench.contract import LoadCell, SweepAggregationError, to_numeric_metric
from slipstream_bench.contract.kv_ab_record import LABELS_KEY, Arm
from slipstream_bench.report.aggregation import get_ceiling


@dataclass(frozen=True)
class KvAbRung:
    """One rung of one arm run: its load cell and the prefix-cache hit rates over it.

    ``gpu_hit_rate`` is the GPU prefix cache's share of the rung's queried tokens (the
    record's ``hit_rate``); ``combined_hit_rate`` adds the tokens the KV connector's CPU
    tier served.
    """

    arm: Arm
    run: int
    cell: LoadCell
    gpu_hit_rate: float
    combined_hit_rate: float

    @classmethod
    def from_record(
        cls, record: dict[str, object], source: Path, *, labels_source: Path
    ) -> "KvAbRung":
        """Read a labelled record into its rung, validating the labels at the seam.

        :param record: the rung's labelled record.
        :param source: where the raw result was read from, for the error message.
        :param labels_source: where the labels were read from, for the error message.
        :return: the rung's arm, run, load cell, and two hit rates.
        :raise SweepAggregationError: when the record lacks its ``kv_ab`` labels or
            carries a bad arm, run, or hit rate, a combined hit rate below its GPU hit
            rate, or its raw result does not parse as a load cell.
        """
        labels = _read_labels(record, source)
        gpu_hit_rate, combined_hit_rate = _read_hit_rates(labels, labels_source)
        return cls(
            arm=_read_arm(labels, labels_source),
            run=_read_run(labels, labels_source),
            cell=LoadCell.from_record(record, source),
            gpu_hit_rate=gpu_hit_rate,
            combined_hit_rate=combined_hit_rate,
        )


class MetricSpread(TypedDict):
    """A metric's median and range across an arm's runs of one rung."""

    median: float
    min: float
    max: float


class KvAbRungSummary(TypedDict):
    """One arm's rung summarised across the runs that measured it."""

    arm: Arm
    max_concurrency: int
    runs: int
    goodput_fraction: MetricSpread
    p95_ttft_ms: MetricSpread
    p95_tpot_ms: MetricSpread
    output_throughput: MetricSpread
    gpu_hit_rate: MetricSpread
    combined_hit_rate: MetricSpread


class KvAbCeiling(TypedDict):
    """One arm run's concurrency ceiling, ``None`` when no rung held the goodput floor."""

    arm: Arm
    run: int
    ceiling: int | None


def summarize_kv_ab_rungs(rungs: list[KvAbRung]) -> list[KvAbRungSummary]:
    """Summarise each arm's rung across its runs as the median and range of each metric.

    :param rungs: every rung of the A/B's arm runs, in any order.
    :return: one spread per arm and rung, off before on, then by rung.
    :raise SweepAggregationError: when the rungs ran more than one prefix share, or an
        arm run's rung is recorded twice.
    """
    _check_rungs(rungs)
    groups: dict[tuple[Arm, int], list[KvAbRung]] = defaultdict(list)
    for rung in rungs:
        groups[(rung.arm, rung.cell.max_concurrency)].append(rung)
    return [
        _get_rung_spread(arm, max_concurrency, group)
        for (arm, max_concurrency), group in sorted(groups.items())
    ]


def get_kv_ab_ceilings(rungs: list[KvAbRung]) -> list[KvAbCeiling]:
    """Read each arm run's concurrency ceiling off its ladder.

    :param rungs: every rung of the A/B's arm runs, in any order.
    :return: one ceiling per arm run, off before on, then by run.
    :raise SweepAggregationError: when the rungs ran more than one prefix share, or an
        arm run's rung is recorded twice.
    """
    _check_rungs(rungs)
    ladders: dict[tuple[Arm, int], list[LoadCell]] = defaultdict(list)
    for rung in rungs:
        ladders[(rung.arm, rung.run)].append(rung.cell)
    ceilings: list[KvAbCeiling] = []
    for (arm, run), cells in sorted(ladders.items()):
        held = get_ceiling(cells)
        ceiling = held.max_concurrency if held else None
        ceilings.append({"arm": arm, "run": run, "ceiling": ceiling})
    return ceilings


def _check_rungs(rungs: list[KvAbRung]) -> None:
    """Refuse rungs of more than one workload, or an arm run's rung recorded twice.

    Either would fold into an arm's spread and ceiling unremarked: rungs of two prefix
    shares as one workload, a rung recorded twice as an extra run.
    """
    shares = sorted({rung.cell.prefix_share for rung in rungs})
    if len(shares) > 1:
        raise SweepAggregationError(
            f"kv-ab rungs ran prefix shares {', '.join(map(str, shares))}: an A/B folds "
            f"one workload"
        )
    seen: set[tuple[Arm, int, int]] = set()
    for rung in rungs:
        key = (rung.arm, rung.run, rung.cell.max_concurrency)
        if key in seen:
            raise SweepAggregationError(
                f"{rung.arm} arm run {rung.run} records its "
                f"--max-concurrency {rung.cell.max_concurrency} rung twice"
            )
        seen.add(key)


def _get_rung_spread(
    arm: Arm, max_concurrency: int, group: list[KvAbRung]
) -> KvAbRungSummary:
    """Summarise one arm's rung across the runs in ``group``."""
    return {
        "arm": arm,
        "max_concurrency": max_concurrency,
        "runs": len(group),
        "goodput_fraction": _get_metric_spread(
            group, lambda rung: rung.cell.goodput_fraction
        ),
        "p95_ttft_ms": _get_metric_spread(group, lambda rung: rung.cell.p95_ttft_ms),
        "p95_tpot_ms": _get_metric_spread(group, lambda rung: rung.cell.p95_tpot_ms),
        "output_throughput": _get_metric_spread(
            group, lambda rung: rung.cell.output_throughput
        ),
        "gpu_hit_rate": _get_metric_spread(group, lambda rung: rung.gpu_hit_rate),
        "combined_hit_rate": _get_metric_spread(
            group, lambda rung: rung.combined_hit_rate
        ),
    }


def _get_metric_spread(
    group: list[KvAbRung], metric: Callable[[KvAbRung], float]
) -> MetricSpread:
    """Read a metric's median and range across the runs in ``group``."""
    values = [metric(rung) for rung in group]
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def _read_labels(record: dict[str, object], source: Path) -> dict[str, object]:
    """Read a record's ``kv_ab`` labels, refusing a record that carries none."""
    labels = record.get(LABELS_KEY)
    if not isinstance(labels, dict):
        raise SweepAggregationError(
            f"result {source} has no {LABELS_KEY} labels: it is not a kv-ab rung record"
        )
    return cast(dict[str, object], labels)


def _read_arm(labels: dict[str, object], source: Path) -> Arm:
    """Read a rung's arm, refusing anything but off or on."""
    arm = labels.get("arm")
    arms = get_args(Arm)
    if arm not in arms:
        raise SweepAggregationError(
            f"result {source} has arm {arm!r}, not {' or '.join(arms)}"
        )
    return cast(Arm, arm)


def _read_run(labels: dict[str, object], source: Path) -> int:
    """Read a rung's 1-based run, refusing a non-integer or a run below 1."""
    run = labels.get("run")
    if isinstance(run, bool) or not isinstance(run, int) or run < 1:
        raise SweepAggregationError(
            f"result {source} has run {run!r}, not a 1-based repetition"
        )
    return run


def _read_hit_rates(labels: dict[str, object], source: Path) -> tuple[float, float]:
    """Read a rung's GPU and combined hit rates, refusing a combined rate below the GPU's.

    The combined rate counts the GPU hits plus the CPU tier's, so it never reads lower.
    """
    gpu_hit_rate = _read_rate(labels, source, "hit_rate")
    combined_hit_rate = _read_rate(labels, source, "combined_hit_rate")
    if combined_hit_rate < gpu_hit_rate:
        raise SweepAggregationError(
            f"result {source} has combined_hit_rate {combined_hit_rate} below its "
            f"hit_rate {gpu_hit_rate}"
        )
    return gpu_hit_rate, combined_hit_rate


def _read_rate(labels: dict[str, object], source: Path, name: str) -> float:
    """Read a hit rate, refusing one outside [0, 1]."""
    rate = to_numeric_metric(labels, source, name, error_cls=SweepAggregationError)
    if rate > 1:
        raise SweepAggregationError(f"result {source} has {name} {rate} above 1")
    return rate
