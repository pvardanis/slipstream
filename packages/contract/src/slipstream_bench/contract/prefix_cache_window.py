"""Read a prefix-cache window: vLLM's cache counters advanced between two /metrics snapshots.

vLLM exposes cumulative prefix-cache counters on its Prometheus /metrics endpoint:
vllm:prefix_cache_queries and vllm:prefix_cache_hits, both counters over the whole
server life, so their bare ratio is polluted by every prior request. A window's true
rate is the delta between a snapshot taken before it and one taken after:
(hits_after - hits_before) / (queries_after - queries_before). This guards a zero or
backwards window (counter restart) and a hits-exceed-queries window, so a caller never
reports a rate the counters cannot support.

The module stays out of the kernel's package surface: its Prometheus parser is a
dependency only the callers that read /metrics need.
"""

import math
from dataclasses import dataclass

from prometheus_client.parser import text_string_to_metric_families

_QUERIES_METRIC = "vllm:prefix_cache_queries"
_HITS_METRIC = "vllm:prefix_cache_hits"


class PrefixCacheError(Exception):
    """A prefix-cache input that cannot produce a meaningful hit rate."""


@dataclass(frozen=True)
class PrefixCacheWindow:
    """The prefix-cache counters' advance over one measured window.

    :param queries: prompt tokens the GPU prefix cache was queried for.
    :param hits: of those, tokens found in the GPU prefix cache.
    """

    queries: int | float
    hits: int | float

    @property
    def hit_rate(self) -> float:
        """The fraction of the window's queried tokens the GPU prefix cache served."""
        return self.hits / self.queries


def _sum_counter(exposition: str, source: str, metric: str, model: str) -> float:
    """Sum a counter's value across the series carrying the run's model_name.

    prometheus_client renders a counter with a _total suffix in the exposition
    (vllm:prefix_cache_queries_total) while vLLM's PromQL name omits it, so match
    either sample name. Summing only the run's model_name series keeps a
    multi-model server's other series out of the count.

    :param exposition: the parsed /metrics text.
    :param source: where the snapshot came from, for the error message.
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


def _get_window_delta(
    before: str,
    after: str,
    sources: tuple[str, str],
    metric: str,
    model: str,
) -> float:
    """Return a counter's advance over the window, before subtracted from after.

    :param before: the exposition text captured before the window.
    :param after: the exposition text captured after the window.
    :param sources: where the (before, after) snapshots came from, for error messages.
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


def get_prefix_cache_window(
    before: str,
    after: str,
    *,
    model: str,
    sources: tuple[str, str] = ("before", "after"),
) -> PrefixCacheWindow:
    """Read the prefix-cache counters' advance between two /metrics snapshots.

    :param before: the /metrics exposition text captured just before the window.
    :param after: the /metrics exposition text captured just after the window.
    :param model: the model_name label to select.
    :param sources: where the (before, after) snapshots came from, for error messages.
    :return: the window's counter deltas.
    :raise PrefixCacheError: on an absent/disabled metric, a non-finite, backwards,
        empty, or hits-exceed-queries counter window, or an unparseable snapshot.
    """
    queries = _get_window_delta(before, after, sources, _QUERIES_METRIC, model)
    hits = _get_window_delta(before, after, sources, _HITS_METRIC, model)

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
            f"({_as_count(queries)}) over the window for model {model!r} "
            f"(counter reset mid-run or wrong model selected?)"
        )
    return PrefixCacheWindow(queries=_as_count(queries), hits=_as_count(hits))
