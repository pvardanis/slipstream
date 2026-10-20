"""Fold a kv-cache-extension-ab run's labelled records into per-arm rung spreads and ceilings.

The KV cache offload A/B (#326) writes one labelled record per rung of each arm run: the
raw ``vllm bench serve`` result plus a ``kv_ab`` block naming its arm and run and its
prefix-cache hit rates (:mod:`slipstream_bench.contract.kv_ab_record`). A record reads into
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
from typing import Any, TypedDict, cast

from slipstream_bench.contract import LoadCell, SweepAggregationError, to_numeric_metric
from slipstream_bench.contract.kv_ab_record import Arm
from slipstream_bench.report.aggregation import get_ceiling

# The key the A/B's labels sit under, beside the raw result's own keys.
_LABELS_KEY = "kv_ab"


@dataclass(frozen=True)
class KvAbRung:
    """One rung of one arm run: its load cell and the prefix-cache hit rates over it.

    ``hit_rate`` is the GPU prefix cache's share of the rung's queried tokens;
    ``combined_hit_rate`` adds the tokens the KV connector's CPU tier served.
    """

    arm: Arm
    run: int
    cell: LoadCell
    hit_rate: float
    combined_hit_rate: float

    @classmethod
    def from_record(cls, record: dict[str, object], source: Path) -> "KvAbRung":
        """Read a labelled record into its rung, validating the labels at the seam.

        :param record: the rung's labelled record.
        :param source: where the record was read from, for the error message.
        :return: the rung's arm, run, load cell, and two hit rates.
        :raise SweepAggregationError: when the record lacks its ``kv_ab`` labels or
            carries a bad arm, run, or hit rate, or its raw result does not parse as
            a load cell.
        """
        labels = record.get(_LABELS_KEY)
        if not isinstance(labels, dict):
            raise SweepAggregationError(
                f"result {source} has no {_LABELS_KEY} labels: it is not a kv-ab "
                f"rung record"
            )
        labels = cast(dict[str, object], labels)
        return cls(
            arm=_read_arm(labels, source),
            run=_read_run(labels, source),
            cell=LoadCell.from_record(record, source),
            hit_rate=_read_rate(labels, source, "hit_rate"),
            combined_hit_rate=_read_rate(labels, source, "combined_hit_rate"),
        )


class Spread(TypedDict):
    """A metric's median and range across an arm's runs of one rung."""

    median: float
    min: float
    max: float


class KvAbRungSpread(TypedDict):
    """One arm's rung summarised across the runs that measured it."""

    arm: Arm
    max_concurrency: int
    runs: int
    goodput_fraction: Spread
    p95_ttft_ms: Spread
    p95_tpot_ms: Spread
    output_throughput: Spread
    hit_rate: Spread
    combined_hit_rate: Spread


class KvAbCeiling(TypedDict):
    """One arm run's concurrency ceiling, ``None`` when no rung held the goodput floor."""

    arm: Arm
    run: int
    ceiling: int | None


def summarize_kv_ab_rungs(rungs: list[KvAbRung]) -> list[KvAbRungSpread]:
    """Summarise each arm's rung across its runs as the median and range of each metric.

    :param rungs: every rung of the A/B's arm runs, in any order.
    :return: one spread per arm and rung, off before on, then by rung.
    """
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
    """
    ladders: dict[tuple[Arm, int], list[LoadCell]] = defaultdict(list)
    for rung in rungs:
        ladders[(rung.arm, rung.run)].append(rung.cell)
    ceilings: list[KvAbCeiling] = []
    for (arm, run), cells in sorted(ladders.items()):
        held = get_ceiling(cells)
        ceiling = held.max_concurrency if held else None
        ceilings.append({"arm": arm, "run": run, "ceiling": ceiling})
    return ceilings


def _get_rung_spread(
    arm: Arm, max_concurrency: int, group: list[KvAbRung]
) -> KvAbRungSpread:
    """Summarise one arm's rung across the runs in ``group``."""

    def spread(metric: Callable[[KvAbRung], float]) -> Spread:
        values = [metric(rung) for rung in group]
        return {
            "median": statistics.median(values),
            "min": min(values),
            "max": max(values),
        }

    return {
        "arm": arm,
        "max_concurrency": max_concurrency,
        "runs": len(group),
        "goodput_fraction": spread(lambda rung: rung.cell.goodput_fraction),
        "p95_ttft_ms": spread(lambda rung: rung.cell.p95_ttft_ms),
        "p95_tpot_ms": spread(lambda rung: rung.cell.p95_tpot_ms),
        "output_throughput": spread(lambda rung: rung.cell.output_throughput),
        "hit_rate": spread(lambda rung: rung.hit_rate),
        "combined_hit_rate": spread(lambda rung: rung.combined_hit_rate),
    }


def _read_arm(labels: dict[str, Any], source: Path) -> Arm:
    """Read a rung's arm, refusing anything but off or on."""
    arm = labels.get("arm")
    if arm not in ("off", "on"):
        raise SweepAggregationError(f"result {source} has arm {arm!r}, not off or on")
    return arm


def _read_run(labels: dict[str, Any], source: Path) -> int:
    """Read a rung's 1-based run, refusing a non-integer or a run below 1."""
    run = labels.get("run")
    if isinstance(run, bool) or not isinstance(run, int) or run < 1:
        raise SweepAggregationError(
            f"result {source} has run {run!r}, not a 1-based repetition"
        )
    return run


def _read_rate(labels: dict[str, object], source: Path, name: str) -> float:
    """Read a hit rate, refusing one outside [0, 1]."""
    rate = to_numeric_metric(labels, source, name, error_cls=SweepAggregationError)
    if rate > 1:
        raise SweepAggregationError(f"result {source} has {name} {rate} above 1")
    return rate
