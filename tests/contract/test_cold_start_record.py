"""The cold-start breakdown record: a run number and phases that can be a cold start."""

import pytest

from slipstream_bench.contract.cold_start_record import (
    ColdStartBreakdown,
    ColdStartRecordError,
)


@pytest.mark.parametrize("run", [0, -1])
def test_a_run_number_below_one_is_refused(run: int) -> None:
    with pytest.raises(ColdStartRecordError, match=rf"run {run}"):
        ColdStartBreakdown(
            run=run,
            total_s=545.0,
            capacity_wait_s=3.0,
            node_launch_s=47.0,
            image_pull_s=264.0,
        )


def test_the_unattributed_remainder_may_read_slightly_negative() -> None:
    breakdown = ColdStartBreakdown(
        run=1, total_s=10.0, capacity_wait_s=4.0, node_launch_s=4.0, image_pull_s=2.5
    )

    assert breakdown.unattributed_s == -0.5
