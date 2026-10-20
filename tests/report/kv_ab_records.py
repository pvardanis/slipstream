"""Labelled kv-ab rung records and their parsed rungs, shared by the kv-ab report tests.

A record is the raw ``vllm bench serve`` result of a rung at 8 req/s plus the ``kv_ab``
block :func:`slipstream_bench.contract.kv_ab_record.build_kv_ab_record` adds, so the fold,
the chart, and the render all read the shape the A/B writes.
"""

from pathlib import Path
from typing import Any

from slipstream_bench.report.kv_ab_aggregation import KvAbRung

# Where the shared record reads as having come from, for the fold's error messages.
SOURCE = Path("bench/kv-ab/ab1/on-r2/pshare90_burst1.0_mc4.json")


def build_record(
    *,
    arm: str = "on",
    run: int = 2,
    max_concurrency: int = 4,
    goodput: float = 7.6,
    p95_ttft_ms: float = 760.0,
    hit_rate: float = 0.3,
    combined_hit_rate: float = 0.85,
) -> dict[str, Any]:
    """Build a labelled record: a raw result of 8 req/s plus its ``kv_ab`` block."""
    return {
        "max_concurrency": max_concurrency,
        "prefix_share": 90,
        "request_goodput": goodput,
        "request_throughput": 8.0,
        "p95_ttft_ms": p95_ttft_ms,
        "p95_tpot_ms": 34.0,
        "output_throughput": 131.0,
        "errors": ["", ""],
        "kv_ab": {
            "arm": arm,
            "run": run,
            "prefix_cache_queries": 1000,
            "prefix_cache_hits": 300,
            "external_prefix_cache_queries": 700,
            "external_prefix_cache_hits": 550,
            "hit_rate": hit_rate,
            "combined_hit_rate": combined_hit_rate,
        },
    }


def build_rung(arm: str, run: int, max_concurrency: int, **metrics: float) -> KvAbRung:
    """Parse a labelled record of the arm run's rung, its metrics overridden."""
    record = build_record(arm=arm, run=run, max_concurrency=max_concurrency, **metrics)
    return KvAbRung.from_record(record, SOURCE)
