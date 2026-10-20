"""Tests for the kv-ab fold: labelled records into rungs, per-arm spreads, and ceilings.

The KV cache offload A/B writes one labelled record per rung of each arm run. The fold
reads each into a rung, summarises every arm's rung across its runs as the median and
range, and reads each arm run's ceiling off its ladder.
"""

import pytest

from slipstream_bench.contract import SweepAggregationError
from slipstream_bench.report.kv_ab_aggregation import (
    KvAbRung,
    get_kv_ab_ceilings,
    summarize_kv_ab_rungs,
)
from tests.report.kv_ab_records import SOURCE, build_record, build_rung


def test_a_record_reads_into_its_arm_run_rung_and_hit_rates() -> None:
    rung = KvAbRung.from_record(build_record(), SOURCE)

    assert (rung.arm, rung.run) == ("on", 2)
    assert rung.cell.max_concurrency == 4
    assert rung.cell.goodput_fraction == pytest.approx(0.95)
    assert rung.cell.p95_ttft_ms == 760.0
    assert (rung.hit_rate, rung.combined_hit_rate) == (0.3, 0.85)


def test_a_record_without_its_labels_is_refused() -> None:
    record = build_record()
    del record["kv_ab"]

    with pytest.raises(SweepAggregationError, match=f"{SOURCE}.*kv_ab"):
        KvAbRung.from_record(record, SOURCE)


@pytest.mark.parametrize(
    ("label", "value", "message"),
    [
        ("arm", "both", "arm 'both', not off or on"),
        ("run", 0, "run 0, not a 1-based repetition"),
        ("run", True, "run True, not a 1-based repetition"),
        ("run", "2", "run '2', not a 1-based repetition"),
        ("hit_rate", None, "missing or non-numeric metric hit_rate"),
        ("combined_hit_rate", 1.2, "combined_hit_rate 1.2 above 1"),
    ],
)
def test_a_record_with_a_bad_label_is_refused(
    label: str, value: object, message: str
) -> None:
    record = build_record()
    record["kv_ab"][label] = value

    with pytest.raises(SweepAggregationError, match=message):
        KvAbRung.from_record(record, SOURCE)


def test_each_arms_rung_is_summarised_across_its_runs_as_median_and_range() -> None:
    # Given in run order, on before off: the summary orders off first, then by rung.
    rungs = [
        build_rung("on", 1, 1, p95_ttft_ms=310.0, combined_hit_rate=0.86),
        build_rung("off", 1, 1, p95_ttft_ms=630.0, combined_hit_rate=0.31),
        build_rung("off", 2, 1, p95_ttft_ms=655.0, combined_hit_rate=0.30),
        build_rung("off", 3, 1, p95_ttft_ms=640.0, combined_hit_rate=0.32),
        build_rung("off", 1, 4, p95_ttft_ms=1480.0, goodput=5.6),
        build_rung("off", 2, 4, p95_ttft_ms=1550.0, goodput=5.2),
    ]

    spreads = summarize_kv_ab_rungs(rungs)

    assert [(s["arm"], s["max_concurrency"], s["runs"]) for s in spreads] == [
        ("off", 1, 3),
        ("off", 4, 2),
        ("on", 1, 1),
    ]
    off_mc1, off_mc4, on_mc1 = spreads
    assert off_mc1["p95_ttft_ms"] == {"median": 640.0, "min": 630.0, "max": 655.0}
    assert off_mc1["combined_hit_rate"] == {"median": 0.31, "min": 0.30, "max": 0.32}
    # An even run count's median is the mean of the middle two.
    assert off_mc4["p95_ttft_ms"] == {"median": 1515.0, "min": 1480.0, "max": 1550.0}
    assert off_mc4["goodput_fraction"] == pytest.approx(
        {"median": 0.675, "min": 0.65, "max": 0.7}
    )
    assert on_mc1["p95_tpot_ms"] == {"median": 34.0, "min": 34.0, "max": 34.0}
    assert on_mc1["output_throughput"] == {"median": 131.0, "min": 131.0, "max": 131.0}
    assert on_mc1["hit_rate"] == {"median": 0.3, "min": 0.3, "max": 0.3}


def test_each_arm_runs_ceiling_is_its_highest_rung_holding_the_goodput_floor() -> None:
    # 8 req/s throughput: 7.6 goodput holds the 0.95 floor, 7.5 falls below it.
    rungs = [
        build_rung("on", 1, 1, goodput=8.0),
        build_rung("on", 1, 2, goodput=7.5),
        build_rung("on", 1, 4, goodput=7.6),
        build_rung("off", 1, 1, goodput=8.0),
        build_rung("off", 1, 4, goodput=4.0),
        build_rung("off", 2, 1, goodput=7.0),
    ]

    assert get_kv_ab_ceilings(rungs) == [
        {"arm": "off", "run": 1, "ceiling": 1},
        {"arm": "off", "run": 2, "ceiling": None},
        {"arm": "on", "run": 1, "ceiling": 4},
    ]
