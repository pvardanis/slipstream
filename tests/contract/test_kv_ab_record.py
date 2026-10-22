"""Tests for the kv-ab cell labels and record: a cell's arm, run, and window, and its join.

The KV cache offload A/B labels each ladder cell with the arm and run it was measured in
and the prefix-cache counters' advance over the cell, so the charts compare arms off the
cell's own traffic. The labels are stored apart from the raw ``vllm bench serve`` result
and joined to it into the record the report folds.
"""

import pytest

from slipstream_bench.contract.kv_ab_record import (
    KvAbRecordError,
    build_kv_ab_labels,
    build_kv_ab_record,
)
from slipstream_bench.contract.prefix_cache_window import PrefixCacheWindow

_RESULT = {"max_concurrency": 4, "completed": 128, "p95_ttft_ms": 850.0}


def test_the_labels_name_the_arm_run_and_window() -> None:
    window = PrefixCacheWindow(
        queries=1000, hits=600, external_queries=400, external_hits=300
    )

    labels = build_kv_ab_labels(arm="on", run=2, window=window)

    assert labels == {
        "arm": "on",
        "run": 2,
        "prefix_cache_queries": 1000,
        "prefix_cache_hits": 600,
        "external_prefix_cache_queries": 400,
        "external_prefix_cache_hits": 300,
        "hit_rate": 0.6,
        "combined_hit_rate": 0.9,
    }


def test_the_record_keeps_the_raw_result_and_nests_the_labels() -> None:
    labels = {"arm": "on", "run": 2, "hit_rate": 0.6, "combined_hit_rate": 0.9}

    record = build_kv_ab_record(_RESULT, labels)

    assert record == {**_RESULT, "kv_ab": labels}


def test_an_off_arm_cell_labels_zero_external_counters() -> None:
    window = PrefixCacheWindow(
        queries=1000, hits=600, external_queries=0, external_hits=0
    )

    labels = build_kv_ab_labels(arm="off", run=1, window=window)

    assert labels["external_prefix_cache_queries"] == 0
    assert labels["combined_hit_rate"] == labels["hit_rate"] == 0.6


def test_an_off_arm_cell_whose_connector_answered_is_rejected() -> None:
    # The off arm deploys without the LMCache connector, so external traffic means the
    # cell was measured on an offloading engine and would credit the CPU tier to "off".
    window = PrefixCacheWindow(
        queries=1000, hits=600, external_queries=400, external_hits=0
    )

    with pytest.raises(KvAbRecordError, match="off arm"):
        build_kv_ab_labels(arm="off", run=1, window=window)


def test_an_on_arm_cell_the_connector_never_answered_is_rejected() -> None:
    # vLLM asks the connector for every admitted request's GPU-missed tokens, and the GPU
    # never serves a whole prompt, so an on-arm cell with no external queries ran on an
    # engine without the connector and would credit "on" with the off arm's hit rate.
    window = PrefixCacheWindow(
        queries=1000, hits=600, external_queries=0, external_hits=0
    )

    with pytest.raises(KvAbRecordError, match="on arm"):
        build_kv_ab_labels(arm="on", run=1, window=window)
