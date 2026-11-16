"""Summarise a metric measured over a few repeated runs as its median and range.

The repeated measurements — the KV cache A/B's arm runs, the cold-start breakdown's
runs — are too few for a confidence interval, so each reports a metric's spread instead:
the median of its runs and the lowest and highest of them, printed ``median [min–max]``.
"""

import statistics
from collections.abc import Sequence
from typing import TypedDict


class MetricSpread(TypedDict):
    """A metric's median and range across the runs that measured it."""

    median: float
    min: float
    max: float


def get_spread(values: Sequence[float]) -> MetricSpread:
    """Read a metric's median and range across its runs' values.

    :param values: the metric's value in each run, in any order.
    :return: the values' median, lowest, and highest.
    :raise statistics.StatisticsError: when ``values`` is empty.
    """
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def format_spread(spread: MetricSpread, fmt: str) -> str:
    """Print a spread as its median with its range bracketed: ``median [min–max]``.

    :param spread: the spread to print.
    :param fmt: the format each of its three values prints in, e.g. ``"{:.1f}"``.
    :return: the printed spread.
    """
    median, low, high = (fmt.format(spread[key]) for key in ("median", "min", "max"))
    return f"{median} [{low}–{high}]"
