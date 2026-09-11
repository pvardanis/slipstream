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

import math
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families

from slipstream_bench.results import read_result

_QUERIES_METRIC = "vllm:prefix_cache_queries"
_HITS_METRIC = "vllm:prefix_cache_hits"
_CACHE_STATES = ("cold", "warm")
_SLO_METRICS = (
    "request_throughput",
    "request_goodput",
    "p95_ttft_ms",
    "p99_ttft_ms",
    "p95_tpot_ms",
    "p99_tpot_ms",
)


class PrefixCacheError(Exception):
    """A prefix-cache input that cannot produce a meaningful hit rate."""


def _read_exposition(path: str) -> str:
    """Read a Prometheus /metrics snapshot file into its text.

    :param path: the snapshot file curled off /metrics.
    :return: the exposition text.
    :raise PrefixCacheError: when the file is absent or unreadable.
    """
    file = Path(path)
    if not file.is_file():
        raise PrefixCacheError(f"metrics snapshot not found: {path}")
    try:
        return file.read_text(encoding="utf-8")
    except OSError as error:
        raise PrefixCacheError(
            f"cannot read metrics snapshot {path}: {error}"
        ) from error


def _sum_counter(exposition: str, source: str, metric: str, model: str) -> float:
    """Sum a counter's value across the series carrying the run's model_name.

    prometheus_client renders a counter with a _total suffix in the exposition
    (vllm:prefix_cache_queries_total) while vLLM's PromQL name omits it, so match
    either sample name. Summing only the run's model_name series keeps a
    multi-model server's other series out of the count.

    :param exposition: the parsed /metrics text.
    :param source: the snapshot file, for the error message.
    :param metric: the counter's PromQL name (no _total suffix).
    :param model: the model_name label value to select.
    :return: the summed counter value across the matching series.
    :raise PrefixCacheError: when no matching series is present — prefix caching
        disabled or the model never emitted — a nothing-to-join error, not a zero.
    """
    names = (metric, f"{metric}_total")
    total = None
    try:
        families = text_string_to_metric_families(exposition)
        for family in families:
            for sample in family.samples:
                if sample.name in names and sample.labels.get("model_name") == model:
                    total = (total or 0.0) + sample.value
    except ValueError as error:
        raise PrefixCacheError(
            f"cannot parse metrics snapshot {source}: {error}"
        ) from error
    if total is None:
        raise PrefixCacheError(
            f"metric {metric} not found in {source} for model {model!r} "
            f"(is prefix caching enabled on the server?)"
        )
    # NaN and infinities in the exposition parse to floats and would produce a
    # non-finite (NaN or inf) hit rate the window guards do not reliably catch, so
    # reject a non-finite counter here, before the delta.
    if not math.isfinite(total):
        raise PrefixCacheError(
            f"metric {metric} has a non-finite value in {source} for model {model!r}"
        )
    return total


def _window_delta(
    before: str,
    after: str,
    sources: tuple[str, str],
    metric: str,
    model: str,
) -> float:
    """Return a counter's advance over the run window, before subtracted from after.

    :param before: the exposition text captured before the run.
    :param after: the exposition text captured after the run.
    :param sources: the (before, after) snapshot files, for error messages.
    :param metric: the counter's PromQL name (no _total suffix).
    :param model: the model_name label value to select.
    :return: the after-minus-before delta across the matching series.
    :raise PrefixCacheError: when either snapshot lacks the metric (see
        :func:`_sum_counter`).
    """
    before_source, after_source = sources
    return _sum_counter(after, after_source, metric, model) - _sum_counter(
        before, before_source, metric, model
    )


def _as_count(value: float) -> int | float:
    """Render a whole counter value as an int, leaving a fractional one a float.

    :param value: a counter delta.
    :return: the value as an int when it is whole, else unchanged.
    """
    return int(value) if float(value).is_integer() else value


def scrape_prefix_cache(
    *,
    metrics_before: str,
    metrics_after: str,
    result: str,
    cache_state: str,
    model: str | None = None,
) -> dict:
    """Compute the per-run prefix-cache hit rate and join it onto the client JSON.

    :param metrics_before: /metrics snapshot captured just before the run.
    :param metrics_after: /metrics snapshot captured just after the run.
    :param result: the run's ``vllm bench serve --save-result`` JSON.
    :param cache_state: which cache regime this run measured — ``cold`` or ``warm``.
    :param model: the model_name label to select; defaults to the result's
        model_id when omitted.
    :return: the joined record: the window deltas, the hit rate, the cold/warm
        label, and the echoed client SLO numbers.
    :raise PrefixCacheError: on a bad cache-state, an absent/disabled metric, a
        non-finite, backwards, empty, or hits-exceed-queries counter window, a
        missing model selector, a truncated or zero-completed client JSON, or an
        unreadable/unparseable snapshot.
    :raise ResultError: when the result file cannot be read (see
        :func:`slipstream_bench.results.read_result`).
    """
    # The label is the whole basis of the cold-vs-warm distinction; a free-text
    # value would let a typo mislabel a regime, so accept only the two defined.
    if cache_state not in _CACHE_STATES:
        raise PrefixCacheError(
            f"invalid cache-state {cache_state!r}: want one of {_CACHE_STATES}"
        )

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
    if not selected_model:
        raise PrefixCacheError(
            f"could not determine model from {result}: no model_id and no model given"
        )

    before = _read_exposition(metrics_before)
    after = _read_exposition(metrics_after)
    sources = (metrics_before, metrics_after)
    queries = _window_delta(before, after, sources, _QUERIES_METRIC, selected_model)
    hits = _window_delta(before, after, sources, _HITS_METRIC, selected_model)

    # A counter that shrank over the window means the server restarted (or the cache
    # was reset) mid-run; the delta is meaningless and the rate would be a lie.
    if queries < 0 or hits < 0:
        raise PrefixCacheError(
            "prefix cache counters went backwards between snapshots "
            "(server restart mid-run?)"
        )
    # No queries in the window is a 0/0 rate: there is no run to measure.
    if queries == 0:
        raise PrefixCacheError(
            "no prefix cache queries between the snapshots (nothing to measure)"
        )
    # Hits are a subset of queries on a single model, so a hits delta above the
    # queries delta is impossible for a healthy series: a mid-run counter reset that
    # touched one series and not the other, or a selector that folded in a foreign
    # series. Either way the > 1.0 rate would be a lie, so reject it.
    if hits > queries:
        raise PrefixCacheError(
            f"prefix cache hits ({_as_count(hits)}) exceed queries "
            f"({_as_count(queries)}) over the window for model {selected_model!r} "
            f"(counter reset mid-run or wrong model selected?)"
        )

    return {
        "source": result,
        "model_id": record.get("model_id"),
        "cache_state": cache_state,
        "completed": record.get("completed"),
        "prefix_cache_queries": _as_count(queries),
        "prefix_cache_hits": _as_count(hits),
        "prefix_cache_hit_rate": hits / queries,
        "client_metrics": {name: record.get(name) for name in _SLO_METRICS},
    }
