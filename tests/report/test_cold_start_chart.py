"""Tests for the cold-start spread table: each field's median and range as Markdown.

The cold-start-breakdown flow publishes the table to its run page: the six phases in
order, engine init's detail beneath its row, the unattributed remainder, and the total.
"""

from slipstream_bench.report.cold_start_aggregation import summarize_cold_starts
from slipstream_bench.report.cold_start_chart import cold_start_spreads_to_markdown
from tests.report.cold_start_breakdowns import build_breakdown


def test_the_table_prints_each_field_as_its_median_and_range_in_phase_order() -> None:
    summary = summarize_cold_starts(
        [
            build_breakdown(1),
            build_breakdown(2, total_s=600.0, capacity_wait_s=60.0),
            build_breakdown(
                3, total_s=560.0, capacity_wait_s=10.0, graph_capture_s=5.0
            ),
        ]
    )

    assert cold_start_spreads_to_markdown(summary) == (
        "| phase | seconds |\n"
        "| --- | --- |\n"
        "| Capacity wait | 10.0 [3.0–60.0] |\n"
        "| Node launch | 47.0 [47.0–47.0] |\n"
        "| Image pull | 264.0 [264.0–264.0] |\n"
        "| Weight download | 42.2 [42.2–42.2] |\n"
        "| Engine init | 104.3 [104.3–104.3] |\n"
        "| ↳ torch.compile | 44.9 [44.9–44.9] |\n"
        "| ↳ Graph capture | 4.0 [4.0–5.0] |\n"
        "| ↳ Profiling run | 0.1 [0.1–0.1] |\n"
        "| Readiness lag | 7.0 [7.0–7.0] |\n"
        "| Unattributed | 77.5 [75.5–85.5] |\n"
        "| **Total** | **560.0 [545.0–600.0]** |"
    )
