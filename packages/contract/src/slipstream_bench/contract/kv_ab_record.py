"""Label a kv-cache-extension-ab cell's result with its arm, run, and prefix-cache window.

The KV cache offload A/B (#326) records each ladder cell as the raw ``vllm bench serve``
result plus a ``kv_ab`` block: the arm and run the cell was measured in, the four
prefix-cache counters' advance over the cell, the GPU hit rate, and the combined GPU plus
KV connector hit rate. The raw result keys stay at the top level, so the record still
parses as a load cell. The off arm deploys without the LMCache connector, so its
external counters must not advance. The on arm deploys with it, and vLLM asks the
connector for every admitted request's GPU-missed tokens (the GPU never serves a whole
prompt), so its external queries must advance. A record that says otherwise is refused.

Like the prefix-cache window it reads, the module is kept out of the kernel's package
surface: only the A/B's /metrics readers need it.
"""

from collections.abc import Mapping
from typing import Any, Literal

from slipstream_bench.contract.prefix_cache_window import PrefixCacheWindow

# Whether a run offloads KV cache to LMCache's CPU tier.
Arm = Literal["off", "on"]

# The key the A/B's labels sit under, beside the raw result's own keys.
LABELS_KEY = "kv_ab"


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
    :raise KvAbRecordError: when an off-arm cell's external counters advanced, or an
        on-arm cell's external queries did not.
    """
    _reject_counters_contradicting_arm(arm, run, window)
    return {
        **result,
        LABELS_KEY: {
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


def _reject_counters_contradicting_arm(
    arm: Arm, run: int, window: PrefixCacheWindow
) -> None:
    """Reject a window whose KV connector counters contradict the arm's deploy."""
    if arm == "off" and (window.external_queries or window.external_hits):
        raise KvAbRecordError(
            f"off arm run {run} advanced the KV connector's counters "
            f"({window.external_queries} external queries, {window.external_hits} "
            f"external hits): the cell ran on an engine that offloads KV cache"
        )
    if arm == "on" and not window.external_queries:
        raise KvAbRecordError(
            f"on arm run {run} sent the KV connector no queries over "
            f"{window.queries} prefix-cache queries: the cell ran on an engine without "
            f"the LMCache connector; check the vLLM pod's KV_CACHE_OFFLOAD and its "
            f"startup log for the connector"
        )
