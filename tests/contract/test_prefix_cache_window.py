"""Tests for the prefix-cache window: counter deltas between two /metrics snapshots.

The window is the advance of vLLM's GPU and external (KV connector) prefix-cache
counters between a before and an after snapshot, so a cell's reuse is read off its own
traffic rather than the server's lifetime ratio.
"""

from slipstream_bench.contract.prefix_cache_window import get_prefix_cache_window

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
