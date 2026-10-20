"""Render a kv-cache-extension-ab run's rung spreads and arm run ceilings as Markdown tables.

The tables sit beside the A/B's chart on the flow run page: one row per arm's rung with each
metric's median and, bracketed, its range across the runs; and one row per arm with each
run's ceiling. Like :mod:`slipstream_bench.report.chart` the renderers are pure and import no
plotting stack.
"""

import statistics
from collections import defaultdict

from slipstream_bench.report.chart import to_markdown_table
from slipstream_bench.report.kv_ab_aggregation import (
    KvAbCeiling,
    KvAbRungSpread,
    Spread,
)

# The spread table's columns: the arm's rung, its run count, then each metric's spread.
_SPREAD_COLUMNS = (
    "arm",
    "max_concurrency",
    "runs",
    "goodput_fraction",
    "p95_ttft_ms",
    "p95_tpot_ms",
    "output_throughput",
    "gpu_hit_rate",
    "combined_hit_rate",
)

# The formats the metrics print in: fractions to two places, milliseconds whole, token
# rates to one place.
_FRACTION = "{:.2f}"
_MS = "{:.0f}"
_RATE = "{:.1f}"

# How a ceiling table cell reads for an arm run that held no rung; a run an arm has no
# record of is left blank instead.
_NO_CEILING = "none"


def kv_ab_spreads_to_markdown(spreads: list[KvAbRungSpread]) -> str:
    """Render the rung spreads as a Markdown table, each metric as ``median [min–max]``.

    :param spreads: the spreads
        :func:`slipstream_bench.report.kv_ab_aggregation.summarize_kv_ab_rungs` emitted.
    :return: the table: header, separator, one row per arm's rung.
    """
    return to_markdown_table(_SPREAD_COLUMNS, [_spread_cells(s) for s in spreads])


def kv_ab_ceilings_to_markdown(ceilings: list[KvAbCeiling]) -> str:
    """Render the arm runs' ceilings as a Markdown table, one row per arm, a column per run.

    An arm's median ceiling is left blank when any of its runs held no rung, or was not
    recorded: a median of only the runs that held would read higher than the arm
    sustained.

    :param ceilings: the ceilings
        :func:`slipstream_bench.report.kv_ab_aggregation.get_kv_ab_ceilings` emitted.
    :return: the table: header, separator, one row per arm.
    """
    by_arm: dict[str, dict[int, int | None]] = defaultdict(dict)
    for ceiling in ceilings:
        by_arm[ceiling["arm"]][ceiling["run"]] = ceiling["ceiling"]
    runs = sorted({ceiling["run"] for ceiling in ceilings})
    columns = ["arm", *(f"r{run}" for run in runs), "median"]
    body = [
        [
            arm,
            *(_format_ceiling(by_run, run) for run in runs),
            _format_median_ceiling(by_run, runs),
        ]
        for arm, by_run in sorted(by_arm.items())
    ]
    return to_markdown_table(columns, body)


def _spread_cells(spread: KvAbRungSpread) -> list[str]:
    """Print one arm's rung as its spread table row."""
    return [
        spread["arm"],
        str(spread["max_concurrency"]),
        str(spread["runs"]),
        _format_spread(spread["goodput_fraction"], _FRACTION),
        _format_spread(spread["p95_ttft_ms"], _MS),
        _format_spread(spread["p95_tpot_ms"], _MS),
        _format_spread(spread["output_throughput"], _RATE),
        _format_spread(spread["gpu_hit_rate"], _FRACTION),
        _format_spread(spread["combined_hit_rate"], _FRACTION),
    ]


def _format_spread(spread: Spread, fmt: str) -> str:
    """Print a spread as its median with its range bracketed."""
    median, low, high = (fmt.format(spread[key]) for key in ("median", "min", "max"))
    return f"{median} [{low}–{high}]"


def _format_ceiling(by_run: dict[int, int | None], run: int) -> str:
    """Print an arm run's ceiling, that it held no rung, or blank when unrecorded."""
    if run not in by_run:
        return ""
    ceiling = by_run[run]
    return _NO_CEILING if ceiling is None else str(ceiling)


def _format_median_ceiling(by_run: dict[int, int | None], runs: list[int]) -> str:
    """Print an arm's median ceiling, blank when any run held no rung or is unrecorded."""
    held = [ceiling for run in runs if (ceiling := by_run.get(run)) is not None]
    if len(held) < len(runs):
        return ""
    return f"{statistics.median(held):g}"
