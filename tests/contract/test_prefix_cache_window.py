"""Tests for the prefix-cache window: counter deltas between two /metrics snapshots.

The window is the advance of vLLM's GPU and external (KV connector) prefix-cache
counters between a before and an after snapshot, so a cell's reuse is read off its own
traffic rather than the server's lifetime ratio.
"""

import pytest

from slipstream_bench.contract.prefix_cache_window import (
    PrefixCacheError,
    get_prefix_cache_window,
)

MODEL = "Qwen/Qwen3-8B-AWQ"


def _exposition(counters: dict[str, float], model: str = MODEL) -> str:
    lines = []
    for metric, value in counters.items():
        lines.append(f"# TYPE {metric} counter")
        lines.append(f'{metric}_total{{model_name="{model}"}} {value}')
    return "\n".join(lines) + "\n"


def _gpu(queries: float, hits: float) -> dict[str, float]:
    return {"vllm:prefix_cache_queries": queries, "vllm:prefix_cache_hits": hits}


def test_hit_rate_is_the_window_delta_not_the_lifetime_ratio() -> None:
    """1000->1100 queries, 200->210 hits: 10/100 = 0.1, not 210/1100."""
    window = get_prefix_cache_window(
        _exposition(_gpu(1000, 200)), _exposition(_gpu(1100, 210)), model=MODEL
    )

    assert (window.queries, window.hits) == (100, 10)
    assert window.hit_rate == 0.1


def _external(queries: float, hits: float) -> dict[str, float]:
    return {
        "vllm:external_prefix_cache_queries": queries,
        "vllm:external_prefix_cache_hits": hits,
    }


def test_combined_rate_adds_the_external_hits_to_the_gpu_hits() -> None:
    """GPU 30/100 hit; LMCache serves 50 of the 70 GPU misses: combined 80/100."""
    before = _exposition(_gpu(1000, 200) | _external(800, 100))
    after = _exposition(_gpu(1100, 230) | _external(870, 150))

    window = get_prefix_cache_window(before, after, model=MODEL)

    assert (window.external_queries, window.external_hits) == (70, 50)
    assert window.combined_hit_rate == 0.8


def test_a_snapshot_without_external_series_reads_zero_external_traffic() -> None:
    """A /metrics without the external series has a window of 0 external of 0."""
    window = get_prefix_cache_window(
        _exposition(_gpu(1000, 200)), _exposition(_gpu(1100, 210)), model=MODEL
    )

    assert (window.external_queries, window.external_hits) == (0, 0)
    assert window.combined_hit_rate == window.hit_rate == 0.1


def test_a_backwards_external_counter_is_rejected() -> None:
    """External hits 150->100: the connector's counters reset mid-window."""
    before = _exposition(_gpu(1000, 200) | _external(800, 150))
    after = _exposition(_gpu(1100, 230) | _external(870, 100))

    with pytest.raises(PrefixCacheError, match="backwards"):
        get_prefix_cache_window(before, after, model=MODEL)


def test_external_hits_exceeding_external_queries_is_rejected() -> None:
    """The connector cannot load more tokens than the GPU missed: 80 of 70 is a lie."""
    before = _exposition(_gpu(1000, 200) | _external(800, 100))
    after = _exposition(_gpu(1100, 210) | _external(870, 180))

    with pytest.raises(PrefixCacheError, match="external prefix cache hits"):
        get_prefix_cache_window(before, after, model=MODEL)
