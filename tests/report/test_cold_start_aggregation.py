"""Tests for the cold-start fold: a measurement's runs into each field's median and range.

The cold-start-breakdown flow measures several cold starts per run id. The fold
summarises every field of their breakdowns — the total, the six phases, the unattributed
remainder, and engine init's detail — across the runs as the median and range.
"""

import pytest

from slipstream_bench.report.cold_start_aggregation import (
    FIELD_LABELS,
    ColdStartAggregationError,
    summarize_cold_starts,
)
from tests.report.cold_start_breakdowns import build_breakdown


def test_each_field_is_summarised_across_the_runs_as_median_and_range() -> None:
    # Given out of run order: the fold reads values, not positions.
    breakdowns = [
        build_breakdown(2, total_s=600.0, capacity_wait_s=60.0, torch_compile_s=40.0),
        build_breakdown(1, total_s=545.0, capacity_wait_s=3.0, torch_compile_s=44.91),
        build_breakdown(3, total_s=560.0, capacity_wait_s=10.0, torch_compile_s=50.0),
    ]

    summary = summarize_cold_starts(breakdowns)

    assert summary["runs"] == 3
    spreads = summary["spreads"]
    assert list(spreads) == [
        "total_s",
        "capacity_wait_s",
        "node_launch_s",
        "image_pull_s",
        "weight_download_s",
        "engine_init_s",
        "readiness_lag_s",
        "unattributed_s",
        "torch_compile_s",
        "graph_capture_s",
        "profiling_run_s",
    ]
    assert spreads["total_s"] == {"median": 560.0, "min": 545.0, "max": 600.0}
    assert spreads["capacity_wait_s"] == {"median": 10.0, "min": 3.0, "max": 60.0}
    assert spreads["torch_compile_s"] == {"median": 44.91, "min": 40.0, "max": 50.0}
    assert spreads["image_pull_s"] == {"median": 264.0, "min": 264.0, "max": 264.0}
    # Each run's remainder (77.517, 75.517, 85.517) is summarised as recorded, not
    # recomputed from the medians.
    assert spreads["unattributed_s"] == pytest.approx(
        {"median": 77.517, "min": 75.517, "max": 85.517}
    )


def test_an_even_run_count_s_median_is_the_mean_of_the_middle_two() -> None:
    breakdowns = [build_breakdown(1, total_s=540.0), build_breakdown(2, total_s=560.0)]

    spread = summarize_cold_starts(breakdowns)["spreads"]["total_s"]

    assert spread == {"median": 550.0, "min": 540.0, "max": 560.0}


def test_every_summarised_field_is_named_for_the_reports() -> None:
    # A recorded field with no name would drop out of the table; a name with no field
    # would fail it.
    summary = summarize_cold_starts([build_breakdown(1)])

    assert sorted(FIELD_LABELS) == sorted(summary["spreads"])


def test_no_runs_are_refused() -> None:
    with pytest.raises(ColdStartAggregationError, match="no runs"):
        summarize_cold_starts([])


def test_a_run_recorded_twice_is_refused() -> None:
    # A run recorded twice would count as an extra run in every spread.
    breakdowns = [build_breakdown(1), build_breakdown(2), build_breakdown(2)]

    with pytest.raises(ColdStartAggregationError, match="run 2 twice"):
        summarize_cold_starts(breakdowns)
