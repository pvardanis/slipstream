"""Tests for the kv-ab tables: the per-arm rung spreads and the arm runs' ceilings as Markdown.

The A/B's render publishes both tables to the flow run page beside its chart: each rung's
median and range across the runs, and each arm run's ceiling.
"""

from slipstream_bench.report.kv_ab_aggregation import (
    KvAbCeiling,
    KvAbRungSummary,
    MetricSpread,
)
from slipstream_bench.report.kv_ab_chart import (
    kv_ab_ceilings_to_markdown,
    kv_ab_spreads_to_markdown,
)


def _spread(median: float, low: float, high: float) -> MetricSpread:
    return {"median": median, "min": low, "max": high}


def test_each_spread_renders_as_its_median_and_range() -> None:
    spread: KvAbRungSummary = {
        "arm": "on",
        "max_concurrency": 4,
        "runs": 3,
        "goodput_fraction": _spread(0.97, 0.95, 0.98),
        "p95_ttft_ms": _spread(760.0, 730.4, 820.6),
        "p95_tpot_ms": _spread(34.0, 32.0, 36.0),
        "output_throughput": _spread(131.0, 126.04, 135.0),
        "gpu_hit_rate": _spread(0.27, 0.26, 0.28),
        "combined_hit_rate": _spread(0.81, 0.8, 0.82),
    }

    assert kv_ab_spreads_to_markdown([spread]).splitlines() == [
        (
            "| arm | max_concurrency | runs | goodput_fraction | p95_ttft_ms "
            "| p95_tpot_ms | output_throughput | gpu_hit_rate | combined_hit_rate |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        (
            "| on | 4 | 3 | 0.97 [0.95–0.98] | 760 [730–821] | 34 [32–36] "
            "| 131.0 [126.0–135.0] | 0.27 [0.26–0.28] | 0.81 [0.80–0.82] |"
        ),
    ]


def test_the_ceilings_render_one_row_per_arm_with_a_column_per_run() -> None:
    ceilings: list[KvAbCeiling] = [
        {"arm": "off", "run": 1, "ceiling": 2},
        {"arm": "off", "run": 2, "ceiling": 1},
        {"arm": "off", "run": 3, "ceiling": 2},
        {"arm": "on", "run": 1, "ceiling": 4},
        {"arm": "on", "run": 2, "ceiling": None},
        {"arm": "on", "run": 3, "ceiling": 4},
    ]

    # A run that held no rung has no ceiling, so its arm's median is left blank rather
    # than read off the runs that held.
    assert kv_ab_ceilings_to_markdown(ceilings).splitlines() == [
        "| arm | r1 | r2 | r3 | median |",
        "| --- | --- | --- | --- | --- |",
        "| off | 2 | 1 | 2 | 2 |",
        "| on | 4 | none | 4 |  |",
    ]


def test_a_run_an_arm_has_no_record_of_is_left_blank_with_its_median() -> None:
    ceilings: list[KvAbCeiling] = [
        {"arm": "off", "run": 1, "ceiling": 2},
        {"arm": "off", "run": 2, "ceiling": 2},
        {"arm": "on", "run": 1, "ceiling": 4},
    ]

    assert kv_ab_ceilings_to_markdown(ceilings).splitlines()[2:] == [
        "| off | 2 | 2 | 2 |",
        "| on | 4 |  |  |",
    ]


def test_an_even_count_of_runs_prints_the_mean_of_its_middle_two_ceilings() -> None:
    ceilings: list[KvAbCeiling] = [
        {"arm": "off", "run": 1, "ceiling": 1},
        {"arm": "off", "run": 2, "ceiling": 2},
        {"arm": "on", "run": 1, "ceiling": 2},
        {"arm": "on", "run": 2, "ceiling": 4},
    ]

    assert kv_ab_ceilings_to_markdown(ceilings).splitlines()[2:] == [
        "| off | 1 | 2 | 1.5 |",
        "| on | 2 | 4 | 3 |",
    ]
