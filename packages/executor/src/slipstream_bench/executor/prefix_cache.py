"""Compute a bench run's prefix-cache hit rate and join it onto the client JSON.

vLLM exposes cumulative prefix-cache counters on its Prometheus /metrics endpoint:
vllm:prefix_cache_queries and vllm:prefix_cache_hits, both counters over the whole
server life, so their bare ratio is polluted by every prior request. A run's true
rate is the delta over the run window: the caller curls /metrics before and after
the run into two snapshots and hands them here with the run's client JSON. This
computes (hits_after - hits_before) / (queries_after - queries_before), guards a
zero or backwards window (counter restart), and joins the rate onto the client
record with a cold/warm label (ADR-0003) so the two cache regimes are told apart.
The run's SLO numbers ride on the record too, so a reader can weigh cold against
warm at the SLO from the record alone, without reopening the client JSON. The
output is a pure function of its inputs — a re-run reproduces the record.

Cold vs warm is the caller's doing: reset the prefix cache before the cold run,
reuse the warmed cache for the warm run, and pass the matching cache_state.
"""

from enum import StrEnum
from pathlib import Path

from slipstream_bench.contract import read_result
from slipstream_bench.contract.prefix_cache_window import (
    PrefixCacheError,
    get_prefix_cache_window,
)

_SLO_METRICS = (
    "request_throughput",
    "request_goodput",
    "p95_ttft_ms",
    "p99_ttft_ms",
    "p95_tpot_ms",
    "p99_tpot_ms",
)


class CacheState(StrEnum):
    """Which cache regime a prefix-cache run measured.

    The two labels are the whole cold-vs-warm basis; typing the CLI option as this
    enum lets Typer reject any other value at the boundary, so the join never has to
    guard a free-text regime.
    """

    cold = "cold"
    warm = "warm"


def _read_metrics_snapshot(path: Path) -> str:
    """Read a vLLM Prometheus /metrics snapshot file into its exposition text.

    :param path: the snapshot file curled off vLLM's /metrics endpoint.
    :return: the exposition text.
    :raise PrefixCacheError: when the file is absent or unreadable.
    """
    if not path.is_file():
        raise PrefixCacheError(f"metrics snapshot not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise PrefixCacheError(
            f"cannot read metrics snapshot {path}: {error}"
        ) from error


def scrape_prefix_cache(
    *,
    metrics_before: Path,
    metrics_after: Path,
    result: Path,
    cache_state: CacheState,
    model: str | None = None,
) -> dict[str, object]:
    """Compute the per-run prefix-cache hit rate and join it onto the client JSON.

    :param metrics_before: vLLM Prometheus /metrics snapshot captured just before
        the run.
    :param metrics_after: vLLM Prometheus /metrics snapshot captured just after the
        run.
    :param result: the run's ``vllm bench serve --save-result`` JSON.
    :param cache_state: which cache regime this run measured — ``cold`` or ``warm``.
    :param model: the model_name label to select; defaults to the result's
        model_id when omitted.
    :return: the joined record — the window counter deltas, the hit rate, the
        cold/warm label, and the echoed client SLO numbers, e.g.::

            {
              "source": "bench/results/prefix-cache/cold_pshare90_burst1.0.json",
              "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
              "cache_state": "cold",
              "request_rate": 8.0,
              "prefix_share": 90,
              "completed": 16,
              "prefix_cache_queries": 4096,
              "prefix_cache_hits": 0,
              "prefix_cache_hit_rate": 0.0,
              "client_metrics": {
                "request_throughput": 0.116,
                "request_goodput": 0.0,
                "p95_ttft_ms": 131084.9,
                "p99_ttft_ms": 131131.9,
                "p95_tpot_ms": 8449.7,
                "p99_tpot_ms": 8619.0
              }
            }

    :raise PrefixCacheError: on an absent/disabled metric, a
        non-finite, backwards, empty, or hits-exceed-queries counter window, a
        missing model selector, a truncated or zero-completed client JSON, or an
        unreadable/unparseable snapshot.
    :raise ResultError: when the result file cannot be read (see
        :func:`slipstream_bench.contract.results.read_result`).
    """
    record = read_result(result)
    # A per-cell failure can leave a syntactically-valid but empty/stub result JSON;
    # the join would then emit null model_id behind a real-looking rate. A run that
    # completed zero requests measured nothing on the client side, so on a shared
    # server its non-zero counter deltas must not be dressed up as a run either.
    if record.get("model_id") is None:
        raise PrefixCacheError(
            f"result {result} missing model_id (truncated or empty run?)"
        )
    if not record.get("completed"):
        raise PrefixCacheError(
            f"result {result} completed no requests (truncated or empty run?)"
        )

    # Default the series selector to the model the client ran against, so a
    # multi-model server's other series never fold into this run's counters.
    selected_model = model or record.get("model_id")
    if not isinstance(selected_model, str) or not selected_model:
        raise PrefixCacheError(
            f"could not determine model from {result}: model_id {selected_model!r} "
            f"is not a usable string and no model given"
        )

    window = get_prefix_cache_window(
        _read_metrics_snapshot(metrics_before),
        _read_metrics_snapshot(metrics_after),
        model=selected_model,
        sources=(str(metrics_before), str(metrics_after)),
    )

    return {
        "source": str(result),
        "model_id": record.get("model_id"),
        "cache_state": cache_state,
        # The segment keys the baseline report groups on (see report.py); a raw
        # result missing either echoes null, and the report rejects a spine record
        # that reaches it without both.
        "request_rate": record.get("request_rate"),
        "prefix_share": record.get("prefix_share"),
        "completed": record.get("completed"),
        "prefix_cache_queries": window.queries,
        "prefix_cache_hits": window.hits,
        # The run's hit rate: the fraction of this window's prefix-cache queries
        # that hit, the whole point of the scrape.
        "prefix_cache_hit_rate": window.hit_rate,
        "client_metrics": {name: record.get(name) for name in _SLO_METRICS},
    }
