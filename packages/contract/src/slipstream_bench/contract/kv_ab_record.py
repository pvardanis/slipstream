"""Label a kv-cache-extension-ab cell's result with its arm, run, and prefix-cache window.

The KV cache offload A/B (#326) records each ladder cell as the raw ``vllm bench serve``
result plus a ``kv_ab`` block: the arm and run the cell was measured in, the four
prefix-cache counters' advance over the cell, the GPU hit rate, and the combined GPU plus
KV connector hit rate. The raw result keys stay at the top level, so the record still
parses as a load cell. The off arm deploys without the LMCache connector, so its
external counters must not advance; a record that says otherwise is refused.

The module stays out of the kernel's package surface with the window it reads.
"""

from collections.abc import Mapping
from typing import Any, Literal

from slipstream_bench.contract.prefix_cache_window import PrefixCacheWindow

# Whether a run offloads KV cache to LMCache's CPU tier.
Arm = Literal["off", "on"]

# The key the A/B's labels sit under, beside the raw result's own keys.
_LABELS_KEY = "kv_ab"


class KvAbRecordError(Exception):
    """A kv-ab cell whose labels contradict its arm."""


def build_kv_ab_record(
    result: Mapping[str, Any], *, arm: Arm, run: int, window: PrefixCacheWindow
) -> dict[str, Any]:
    """Label a cell's raw result with its arm, run, and prefix-cache window.

    :param result: the cell's ``vllm bench serve`` result record.
    :param arm: the arm the cell was measured in.
    :param run: the run's 1-based repetition within its arm.
    :param window: the prefix-cache counters' advance over the cell.
    :return: the result with a ``kv_ab`` block of the labels, the four counter deltas,
        and the two hit rates.
    :raise KvAbRecordError: when an off-arm cell's external counters advanced.
    """
    if arm == "off" and (window.external_queries or window.external_hits):
        raise KvAbRecordError(
            f"off arm run {run} advanced the KV connector's counters "
            f"({window.external_queries} external queries, {window.external_hits} "
            f"external hits): the cell ran on an engine that offloads KV cache"
        )
    return {
        **result,
        _LABELS_KEY: {
            "arm": arm,
            "run": run,
            "prefix_cache_queries": window.queries,
            "prefix_cache_hits": window.hits,
            "external_prefix_cache_queries": window.external_queries,
            "external_prefix_cache_hits": window.external_hits,
            "hit_rate": window.hit_rate,
            "combined_hit_rate": window.combined_hit_rate,
        },
    }
