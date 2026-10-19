"""Read a prefix-cache window: vLLM's cache counters advanced between two /metrics snapshots.

vLLM exposes cumulative prefix-cache counters on its Prometheus /metrics endpoint:
vllm:prefix_cache_queries and vllm:prefix_cache_hits, both counters over the whole
server life, so their bare ratio is polluted by every prior request. A window's true
rate is the delta between a snapshot taken before it and one taken after:
(hits_after - hits_before) / (queries_after - queries_before). A KV connector such as
LMCache adds vllm:external_prefix_cache_queries/hits for the tokens the GPU missed, so
the window also carries their deltas and a combined rate, (GPU hits + external hits) /
queries. This guards a zero or backwards window (counter restart) and a
hits-exceed-queries window, so a caller never reports a rate the counters cannot
support.

The module stays out of the kernel's package surface: its Prometheus parser is a
dependency only the callers that read /metrics need.
"""

import math
from dataclasses import dataclass

from prometheus_client.parser import text_string_to_metric_families

_QUERIES_METRIC = "vllm:prefix_cache_queries"
_HITS_METRIC = "vllm:prefix_cache_hits"
_EXTERNAL_QUERIES_METRIC = "vllm:external_prefix_cache_queries"
_EXTERNAL_HITS_METRIC = "vllm:external_prefix_cache_hits"
_GPU_METRICS = (_QUERIES_METRIC, _HITS_METRIC)
_EXTERNAL_METRICS = (_EXTERNAL_QUERIES_METRIC, _EXTERNAL_HITS_METRIC)


class PrefixCacheError(Exception):
    """A prefix-cache input that cannot produce a meaningful hit rate."""


@dataclass(frozen=True)
class PrefixCacheWindow:
    """The prefix-cache counters' advance over one measured window.

    :param queries: prompt tokens the GPU prefix cache was queried for.
    :param hits: of those, tokens found in the GPU prefix cache.
    :param external_queries: tokens the GPU missed and the KV connector was asked for.
    :param external_hits: of those, tokens the KV connector loaded.
    """

    queries: int | float
    hits: int | float
    external_queries: int | float
    external_hits: int | float

    @property
    def hit_rate(self) -> float:
        """The fraction of the window's queried tokens the GPU prefix cache served."""
        return self.hits / self.queries

    @property
    def combined_hit_rate(self) -> float:
        """The fraction of the window's queried tokens the GPU or the KV connector served."""
        return (self.hits + self.external_hits) / self.queries


def _sum_counters(exposition: str, source: str, model: str) -> dict[str, float]:
    """Sum each prefix-cache counter across the series carrying the run's model_name.

    prometheus_client renders a counter with a _total suffix in the exposition
    (vllm:prefix_cache_queries_total) while vLLM's PromQL name omits it, so match
    either sample name. Summing only the run's model_name series keeps a
    multi-model server's other series out of the count.

    :param exposition: the parsed /metrics text.
    :param source: where the snapshot came from, for the error message.
    :param model: the model_name label value to select.
    :return: the summed value per counter PromQL name, holding only the counters
        the snapshot carries.
    :raise PrefixCacheError: when the snapshot cannot be parsed.
    """
    totals: dict[str, float] = {}
    try:
        families = text_string_to_metric_families(exposition)
        for family in families:
            for sample in family.samples:
                name = sample.name.removesuffix("_total")
                wanted = name in _GPU_METRICS or name in _EXTERNAL_METRICS
                if wanted and sample.labels.get("model_name") == model:
                    totals[name] = totals.get(name, 0.0) + sample.value
    except ValueError as error:
        raise PrefixCacheError(
            f"cannot parse metrics snapshot {source}: {error}"
        ) from error
    return totals


def _read_snapshot(exposition: str, source: str, model: str) -> dict[str, float]:
    """Read one /metrics snapshot's prefix-cache counters for the run's model.

    :param exposition: the /metrics exposition text.
    :param source: where the snapshot came from, for error messages.
    :param model: the model_name label value to select.
    :return: the summed value per counter PromQL name; the external counters are
        present only when the snapshot carries them.
    :raise PrefixCacheError: when a GPU counter is absent — prefix caching disabled
        or the model never emitted — a nothing-to-join error, not a zero; or when a
        counter is non-finite or the snapshot cannot be parsed.
    """
    totals = _sum_counters(exposition, source, model)
    for metric in _GPU_METRICS:
        if metric not in totals:
            raise PrefixCacheError(
                f"metric {metric} not found in {source} for model {model!r} "
                f"(is prefix caching enabled on the server?)"
            )
    # NaN and infinities in the exposition parse to floats and would produce a
    # non-finite (NaN or inf) hit rate the window guards do not reliably catch, so
    # reject a non-finite counter here, before the delta.
    for metric, total in totals.items():
        if not math.isfinite(total):
            raise PrefixCacheError(
                f"metric {metric} has a non-finite value in {source} "
                f"for model {model!r}"
            )
    return totals


def _get_window_deltas(
    before: dict[str, float], after: dict[str, float]
) -> dict[str, float]:
    """Return each counter's advance over the window, before subtracted from after.

    vLLM registers the external counters on every server, at zero without a KV
    connector; a snapshot pair without them (an older server) still carries a GPU
    hit rate, so absent external counters read as zero rather than failing the
    window. External counters in only one of the two snapshots mean the server
    restarted in between, and the delta would be a lifetime value or negative.

    :param before: the counters read off the snapshot captured before the window.
    :param after: the counters read off the snapshot captured after the window.
    :return: the after-minus-before delta per counter PromQL name.
    :raise PrefixCacheError: when the external counters appear in only one snapshot.
    """
    for metric in _EXTERNAL_METRICS:
        if (metric in before) != (metric in after):
            raise PrefixCacheError(
                f"metric {metric} is in only one snapshot of the window "
                f"(server restart mid-run?)"
            )
    return {
        metric: after.get(metric, 0.0) - before.get(metric, 0.0)
        for metric in _GPU_METRICS + _EXTERNAL_METRICS
    }


def _as_count(value: float) -> int | float:
    """Render a whole counter value as an int, leaving a fractional one a float.

    :param value: a counter delta.
    :return: the value as an int when it is whole, else unchanged.
    """
    return int(value) if float(value).is_integer() else value


def _check_window(deltas: dict[str, float], model: str) -> None:
    """Reject a window whose counter deltas cannot support a hit rate.

    :param deltas: the after-minus-before delta per counter PromQL name.
    :param model: the model_name label the window selected, for error messages.
    :raise PrefixCacheError: on a backwards, empty, or hits-exceed-queries window,
        GPU or external.
    """
    queries, hits = deltas[_QUERIES_METRIC], deltas[_HITS_METRIC]
    external_queries = deltas[_EXTERNAL_QUERIES_METRIC]
    external_hits = deltas[_EXTERNAL_HITS_METRIC]
    # A counter that shrank over the window means the server restarted (or the cache
    # was reset) mid-run; the delta is meaningless and the rate would be a lie.
    if min(deltas.values()) < 0:
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
    # The connector is asked only for the tokens the GPU missed and loads at most
    # those, so external hits above external queries break the same subset rule.
    if external_hits > external_queries:
        raise PrefixCacheError(
            f"external prefix cache hits ({_as_count(external_hits)}) exceed "
            f"external queries ({_as_count(external_queries)}) over the window for "
            f"model {model!r} (counter reset mid-run or wrong model selected?)"
        )


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
    :raise PrefixCacheError: on an absent/disabled GPU metric, external counters in
        only one snapshot, a non-finite, backwards, empty, or hits-exceed-queries
        counter window (GPU or external), or an unparseable snapshot.
    """
    before_source, after_source = sources
    deltas = _get_window_deltas(
        _read_snapshot(before, before_source, model),
        _read_snapshot(after, after_source, model),
    )
    _check_window(deltas, model)
    return PrefixCacheWindow(
        queries=_as_count(deltas[_QUERIES_METRIC]),
        hits=_as_count(deltas[_HITS_METRIC]),
        external_queries=_as_count(deltas[_EXTERNAL_QUERIES_METRIC]),
        external_hits=_as_count(deltas[_EXTERNAL_HITS_METRIC]),
    )
