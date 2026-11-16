"""Cold-start breakdowns shared by the cold-start report tests.

A breakdown defaults to the recorded true-zero cold start's phases
(tests/fixtures/cold-start), so the fold, the table, and the chart read the shape the
cold-start flow measures; a test overrides the durations it varies across runs.
"""

from slipstream_bench.contract.cold_start_record import ColdStartBreakdown

# The recorded cold start's durations, in seconds.
_RECORDED: dict[str, float] = {
    "total_s": 545.0,
    "capacity_wait_s": 3.0,
    "node_launch_s": 47.0,
    "image_pull_s": 264.0,
    "weight_download_s": 42.213,
    "engine_init_s": 104.27,
    "readiness_lag_s": 7.0,
    "torch_compile_s": 44.91,
    "graph_capture_s": 4.0,
    "profiling_run_s": 0.1,
}


def build_breakdown(run: int, **seconds: float) -> ColdStartBreakdown:
    """Build run ``run``'s breakdown: the recorded durations, ``seconds`` overriding."""
    return ColdStartBreakdown(run=run, **{**_RECORDED, **seconds})
