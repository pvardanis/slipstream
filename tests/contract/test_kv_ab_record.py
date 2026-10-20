"""Tests for the kv-ab cell record: a cell's raw result labelled with its arm, run, and window.

The KV cache offload A/B records each ladder cell as the raw ``vllm bench serve`` result
plus the arm and run it was measured in and the prefix-cache counters' advance over the
cell, so the charts compare arms off the cell's own traffic.
"""

import pytest

from slipstream_bench.contract.kv_ab_record import KvAbRecordError, build_kv_ab_record
from slipstream_bench.contract.prefix_cache_window import PrefixCacheWindow

_RESULT = {"max_concurrency": 4, "completed": 128, "p95_ttft_ms": 850.0}


def test_the_record_keeps_the_raw_result_and_labels_the_arm_run_and_window() -> None:
    window = PrefixCacheWindow(
        queries=1000, hits=600, external_queries=400, external_hits=300
    )

    record = build_kv_ab_record(_RESULT, arm="on", run=2, window=window)

    assert record == {
        **_RESULT,
        "kv_ab": {
            "arm": "on",
            "run": 2,
            "prefix_cache_queries": 1000,
            "prefix_cache_hits": 600,
            "external_prefix_cache_queries": 400,
            "external_prefix_cache_hits": 300,
            "hit_rate": 0.6,
            "combined_hit_rate": 0.9,
        },
    }


def test_an_off_arm_cell_records_zero_external_counters() -> None:
    window = PrefixCacheWindow(
        queries=1000, hits=600, external_queries=0, external_hits=0
    )

    labels = build_kv_ab_record(_RESULT, arm="off", run=1, window=window)["kv_ab"]

    assert labels["external_prefix_cache_queries"] == 0
    assert labels["combined_hit_rate"] == labels["hit_rate"] == 0.6


def test_an_off_arm_cell_whose_connector_answered_is_rejected() -> None:
    # The off arm deploys without the LMCache connector, so external traffic means the
    # cell was measured on an offloading engine and would credit the CPU tier to "off".
    window = PrefixCacheWindow(
        queries=1000, hits=600, external_queries=400, external_hits=0
    )

    with pytest.raises(KvAbRecordError, match="off arm"):
        build_kv_ab_record(_RESULT, arm="off", run=1, window=window)
