"""Fold a cold-start-breakdown run's breakdowns into each field's median and range.

The cold-start-breakdown flow measures several true-zero cold starts per run id
(ADR-0030), each a :class:`ColdStartBreakdown`. The fold summarises every field of their
records — the total, the six phases, the unattributed remainder, and engine init's
detail — across the runs as its median and range (:func:`summarize_cold_starts`),
since the runs are too few for a confidence interval. It also names each field for the
reports that print it. Each field is summarised on its own, so the phases' medians need
not add up to the total's median. Like the KV cache A/B's fold, the module imports no
plotting stack.
"""

from typing import TypedDict

from slipstream_bench.contract.cold_start_record import ColdStartBreakdown
from slipstream_bench.report.spread import MetricSpread, get_spread

# The record's run-number key, which is a label rather than a duration to summarise.
_RUN_KEY = "run"

# Each record field's name as a reader sees it, in the order a report lists them.
FIELD_LABELS = {
    "capacity_wait_s": "Capacity wait",
    "node_launch_s": "Node launch",
    "image_pull_s": "Image pull",
    "weight_download_s": "Weight download",
    "engine_init_s": "Engine init",
    "torch_compile_s": "torch.compile",
    "graph_capture_s": "Graph capture",
    "profiling_run_s": "Profiling run",
    "readiness_lag_s": "Readiness lag",
    "unattributed_s": "Unattributed",
    "total_s": "Total",
}


class ColdStartAggregationError(Exception):
    """A set of cold-start breakdowns that cannot be summarised as one measurement."""


class ColdStartSummary(TypedDict):
    """A measurement's cold starts summarised across its runs.

    ``spreads`` is keyed by the record's duration fields, in the record's order.
    """

    runs: int
    spreads: dict[str, MetricSpread]


def summarize_cold_starts(breakdowns: list[ColdStartBreakdown]) -> ColdStartSummary:
    """Summarise each duration across the runs as its median and range.

    :param breakdowns: every run's breakdown, in any order.
    :return: the run count and each duration field's spread, in the record's order.
    :raise ColdStartAggregationError: when there are no runs, or a run is recorded twice.
    """
    _check_runs(breakdowns)
    records = [breakdown.to_record() for breakdown in breakdowns]
    fields = [key for key in records[0] if key != _RUN_KEY]
    return {
        "runs": len(records),
        "spreads": {
            field: get_spread([record[field] for record in records]) for field in fields
        },
    }


def _check_runs(breakdowns: list[ColdStartBreakdown]) -> None:
    """Refuse no runs, or a run recorded twice, which would count as an extra run."""
    if not breakdowns:
        raise ColdStartAggregationError("cannot summarise a cold start with no runs")
    seen: set[int] = set()
    for breakdown in breakdowns:
        if breakdown.run in seen:
            raise ColdStartAggregationError(
                f"the cold-start breakdowns record run {breakdown.run} twice"
            )
        seen.add(breakdown.run)
