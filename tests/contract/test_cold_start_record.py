"""The cold-start breakdown record: a run number and phases that can be a cold start."""

from dataclasses import fields, replace

import pytest

from slipstream_bench.contract.cold_start_record import (
    ColdStartBreakdown,
    ColdStartRecordError,
)

# The hand-worked breakdown of the recorded cold start on #37.
_RECORDED = ColdStartBreakdown(
    run=1,
    total_s=545.0,
    capacity_wait_s=3.0,
    node_launch_s=47.0,
    image_pull_s=264.0,
    weight_download_s=42.213,
    engine_init_s=104.27,
    readiness_lag_s=6.596,
    torch_compile_s=44.91,
    graph_capture_s=4.0,
    profiling_run_s=0.1,
)


@pytest.mark.parametrize("run", [0, -1])
def test_a_run_number_below_one_is_refused(run: int) -> None:
    with pytest.raises(ColdStartRecordError, match=rf"run {run}"):
        replace(_RECORDED, run=run)


@pytest.mark.parametrize(
    "name", [field.name for field in fields(ColdStartBreakdown) if field.name != "run"]
)
def test_a_negative_duration_is_refused(name: str) -> None:
    with pytest.raises(ColdStartRecordError, match=rf"{name} is -1\.0"):
        replace(_RECORDED, **{name: -1.0})


def test_the_six_phases_and_unattributed_add_up_to_the_total() -> None:
    assert _RECORDED.unattributed_s == 77.921


def test_engine_init_detail_is_not_subtracted_from_the_total() -> None:
    assert replace(_RECORDED, torch_compile_s=100.0).unattributed_s == 77.921


def test_the_unattributed_remainder_may_read_slightly_negative() -> None:
    assert replace(_RECORDED, total_s=466.579).unattributed_s == -0.5


def test_the_record_carries_each_phase_its_detail_and_the_remainder() -> None:
    assert _RECORDED.to_record() == {
        "run": 1,
        "total_s": 545.0,
        "capacity_wait_s": 3.0,
        "node_launch_s": 47.0,
        "image_pull_s": 264.0,
        "weight_download_s": 42.213,
        "engine_init_s": 104.27,
        "readiness_lag_s": 6.596,
        "unattributed_s": 77.921,
        "torch_compile_s": 44.91,
        "graph_capture_s": 4.0,
        "profiling_run_s": 0.1,
    }
