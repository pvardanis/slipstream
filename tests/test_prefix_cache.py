"""Tests for the prefix-cache-hit scraper: per-run delta hit rate joined to the JSON.

Seeded from the checklist mined off the deleted bash test (ADR-0003): the delta
over the run window rather than the polluted lifetime ratio, the cold/warm label,
the join onto the client JSON with its SLO numbers, per-model_name series
selection, the _total-suffix rendering prometheus_client emits, large counters
without precision loss, run-to-run reproducibility, and the fail-fast guards on a
bad cache-state, an absent/disabled metric, a non-finite counter, a backwards or
asymmetric counter window (server restart), an empty query window, a hits-exceed-
queries window, a missing model selector, a truncated or zero-completed client
JSON, and an unparseable snapshot.
"""

import json
from pathlib import Path

import pytest

from slipstream_bench.prefix_cache import PrefixCacheError, scrape_prefix_cache
from slipstream_bench.results import ResultError

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"


def _result(tmp_path: Path, **extra: object) -> Path:
    record = {
        "model_id": MODEL,
        "duration": 12.5,
        "completed": 100,
        "request_throughput": 8.0,
        "request_goodput": 7.5,
        "p95_ttft_ms": 850.0,
        "p99_ttft_ms": 990.0,
        "p95_tpot_ms": 42.0,
        "p99_tpot_ms": 48.0,
    }
    record.update(extra)
    path = tmp_path / "cell.json"
    path.write_text(json.dumps(record))
    return path


def _snapshot(tmp_path: Path, name: str, queries: object, hits: object) -> Path:
    path = tmp_path / name
    path.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        f'vllm:prefix_cache_queries{{model_name="{MODEL}"}} {queries}\n'
        f'vllm:prefix_cache_hits{{model_name="{MODEL}"}} {hits}\n'
    )
    return path


def test_hit_rate_is_the_window_delta_not_the_lifetime_ratio(tmp_path: Path) -> None:
    """Cold run: 1000->1100 queries, 200->210 hits — 10/100 = 0.1, not 210/1100."""
    before = _snapshot(tmp_path, "before.prom", 1000.0, 200.0)
    after = _snapshot(tmp_path, "after.prom", 1100.0, 210.0)

    record = scrape_prefix_cache(
        metrics_before=before,
        metrics_after=after,
        result=_result(tmp_path),
        cache_state="cold",
    )

    assert record["prefix_cache_queries"] == 100
    assert record["prefix_cache_hits"] == 10
    assert record["prefix_cache_hit_rate"] == pytest.approx(0.1)
    assert record["cache_state"] == "cold"


def test_client_json_and_slo_numbers_ride_on_the_record(tmp_path: Path) -> None:
    """The join carries model_id, completed, source, and the SLO numbers verbatim."""
    result = _result(tmp_path)
    record = scrape_prefix_cache(
        metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
        metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
        result=result,
        cache_state="cold",
    )

    assert record["source"] == str(result)
    assert record["model_id"] == MODEL
    assert record["completed"] == 100
    assert record["client_metrics"] == {
        "request_throughput": 8.0,
        "request_goodput": 7.5,
        "p95_ttft_ms": 850.0,
        "p99_ttft_ms": 990.0,
        "p95_tpot_ms": 42.0,
        "p99_tpot_ms": 48.0,
    }


def test_missing_client_metric_joins_as_null(tmp_path: Path) -> None:
    """A run fired without --goodput has no request_goodput; it joins as null."""
    result = tmp_path / "nogood.json"
    result.write_text(json.dumps({"model_id": MODEL, "completed": 5}))
    record = scrape_prefix_cache(
        metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
        metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
        result=result,
        cache_state="cold",
    )

    assert record["client_metrics"]["request_goodput"] is None


def test_warm_rate_exceeds_cold_and_is_labelled(tmp_path: Path) -> None:
    """Warm window 1100->1200 / 210->300 is 90/100 = 0.9, above the cold rate."""
    record = scrape_prefix_cache(
        metrics_before=_snapshot(tmp_path, "b.prom", 1100.0, 210.0),
        metrics_after=_snapshot(tmp_path, "a.prom", 1200.0, 300.0),
        result=_result(tmp_path),
        cache_state="warm",
    )

    assert record["prefix_cache_queries"] == 100
    assert record["prefix_cache_hits"] == 90
    assert record["prefix_cache_hit_rate"] == pytest.approx(0.9)
    assert record["cache_state"] == "warm"


def test_other_models_series_are_not_folded_in(tmp_path: Path) -> None:
    """A second model's counters share the file; the run's model must not sum them."""
    before = tmp_path / "b.prom"
    before.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        f'vllm:prefix_cache_queries{{model_name="{MODEL}"}} 1000.0\n'
        'vllm:prefix_cache_queries{model_name="other/model"} 5000.0\n'
        f'vllm:prefix_cache_hits{{model_name="{MODEL}"}} 200.0\n'
        'vllm:prefix_cache_hits{model_name="other/model"} 4000.0\n'
    )
    after = tmp_path / "a.prom"
    after.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        f'vllm:prefix_cache_queries{{model_name="{MODEL}"}} 1100.0\n'
        'vllm:prefix_cache_queries{model_name="other/model"} 9999.0\n'
        f'vllm:prefix_cache_hits{{model_name="{MODEL}"}} 210.0\n'
        'vllm:prefix_cache_hits{model_name="other/model"} 8888.0\n'
    )
    record = scrape_prefix_cache(
        metrics_before=before,
        metrics_after=after,
        result=_result(tmp_path),
        cache_state="cold",
    )

    assert record["prefix_cache_queries"] == 100
    assert record["prefix_cache_hits"] == 10


def test_total_suffix_rendering_is_read(tmp_path: Path) -> None:
    """prometheus_client renders a counter with a _total suffix; read that too."""
    before = tmp_path / "b.prom"
    before.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        f'vllm:prefix_cache_queries_total{{model_name="{MODEL}"}} 1000.0\n'
        "# TYPE vllm:prefix_cache_hits counter\n"
        f'vllm:prefix_cache_hits_total{{model_name="{MODEL}"}} 200.0\n'
    )
    after = tmp_path / "a.prom"
    after.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        f'vllm:prefix_cache_queries_total{{model_name="{MODEL}"}} 1100.0\n'
        "# TYPE vllm:prefix_cache_hits counter\n"
        f'vllm:prefix_cache_hits_total{{model_name="{MODEL}"}} 290.0\n'
    )
    record = scrape_prefix_cache(
        metrics_before=before,
        metrics_after=after,
        result=_result(tmp_path),
        cache_state="warm",
    )

    assert record["prefix_cache_queries"] == 100
    assert record["prefix_cache_hits"] == 90


def test_model_override_selects_a_series_the_model_id_would_not(tmp_path: Path) -> None:
    """--model reaches past the result's model_id to a different served-name series."""
    before = tmp_path / "b.prom"
    before.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        'vllm:prefix_cache_queries{model_name="served-name"} 300.0\n'
        f'vllm:prefix_cache_queries{{model_name="{MODEL}"}} 1000.0\n'
        'vllm:prefix_cache_hits{model_name="served-name"} 100.0\n'
        f'vllm:prefix_cache_hits{{model_name="{MODEL}"}} 200.0\n'
    )
    after = tmp_path / "a.prom"
    after.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        'vllm:prefix_cache_queries{model_name="served-name"} 350.0\n'
        f'vllm:prefix_cache_queries{{model_name="{MODEL}"}} 9999.0\n'
        'vllm:prefix_cache_hits{model_name="served-name"} 140.0\n'
        f'vllm:prefix_cache_hits{{model_name="{MODEL}"}} 8888.0\n'
    )
    record = scrape_prefix_cache(
        metrics_before=before,
        metrics_after=after,
        result=_result(tmp_path),
        cache_state="cold",
        model="served-name",
    )

    assert record["prefix_cache_queries"] == 50
    assert record["prefix_cache_hits"] == 40


def test_large_counters_keep_full_integer_precision(tmp_path: Path) -> None:
    """A long-lived server's counters exceed 10 digits; the delta stays exact."""
    before = _snapshot(tmp_path, "b.prom", 12345678901234, 12345678900000)
    after = _snapshot(tmp_path, "a.prom", 12345678901334, 12345678900050)
    record = scrape_prefix_cache(
        metrics_before=before,
        metrics_after=after,
        result=_result(tmp_path),
        cache_state="warm",
    )

    assert record["prefix_cache_queries"] == 100
    assert record["prefix_cache_hits"] == 50


@pytest.mark.parametrize("bad", ["NaN", "+Inf", "-Inf"])
def test_non_finite_counter_is_rejected(tmp_path: Path, bad: str) -> None:
    """The exposition can encode NaN/Inf; a silent NaN hit rate is nonsense, so reject."""
    before = _snapshot(tmp_path, "b.prom", 1000.0, 200.0)
    after = _snapshot(tmp_path, "a.prom", bad, 210.0)
    with pytest.raises(PrefixCacheError, match="non-finite"):
        scrape_prefix_cache(
            metrics_before=before,
            metrics_after=after,
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_same_input_reproduces_identical_output(tmp_path: Path) -> None:
    """A re-run reproduces the joined record byte for byte."""
    before = _snapshot(tmp_path, "b.prom", 1000.0, 200.0)
    after = _snapshot(tmp_path, "a.prom", 1100.0, 210.0)
    result = _result(tmp_path)

    run_a = json.dumps(
        scrape_prefix_cache(
            metrics_before=before,
            metrics_after=after,
            result=result,
            cache_state="cold",
        )
    )
    run_b = json.dumps(
        scrape_prefix_cache(
            metrics_before=before,
            metrics_after=after,
            result=result,
            cache_state="cold",
        )
    )

    assert run_a == run_b


def test_counts_are_emitted_as_integers(tmp_path: Path) -> None:
    """Queries and hits are counts, echoed as ints, not the float parsed internally."""
    record = scrape_prefix_cache(
        metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
        metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
        result=_result(tmp_path),
        cache_state="cold",
    )

    assert isinstance(record["prefix_cache_queries"], int)
    assert isinstance(record["prefix_cache_hits"], int)


@pytest.mark.parametrize("state", ["lukewarm", "", "COLD"])
def test_bad_cache_state_is_rejected(tmp_path: Path, state: str) -> None:
    """The label is the whole cold-vs-warm basis; a free-text value is rejected."""
    with pytest.raises(PrefixCacheError, match="cache-state"):
        scrape_prefix_cache(
            metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=_result(tmp_path),
            cache_state=state,
        )


def test_absent_metric_is_rejected(tmp_path: Path) -> None:
    """Prefix caching disabled means nothing to join, not a 0/0 rate."""
    nocache = tmp_path / "nocache.prom"
    nocache.write_text(
        "# TYPE vllm:num_requests_running gauge\n"
        f'vllm:num_requests_running{{model_name="{MODEL}"}} 0.0\n'
    )
    with pytest.raises(PrefixCacheError, match="vllm:prefix_cache_queries"):
        scrape_prefix_cache(
            metrics_before=nocache,
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_metric_present_only_for_another_model_is_rejected(tmp_path: Path) -> None:
    """A metric present but never for the run's model is a nothing-to-join error."""
    before = tmp_path / "b.prom"
    before.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        'vllm:prefix_cache_queries{model_name="other/model"} 5000.0\n'
        'vllm:prefix_cache_hits{model_name="other/model"} 4000.0\n'
    )
    with pytest.raises(PrefixCacheError, match="vllm:prefix_cache_queries"):
        scrape_prefix_cache(
            metrics_before=before,
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_backwards_counter_window_is_rejected(tmp_path: Path) -> None:
    """A counter that shrank means the server restarted mid-run; the delta is a lie."""
    before = _snapshot(tmp_path, "b.prom", 1000.0, 200.0)
    after = _snapshot(tmp_path, "a.prom", 5.0, 1.0)
    with pytest.raises(PrefixCacheError, match="backwards"):
        scrape_prefix_cache(
            metrics_before=before,
            metrics_after=after,
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_asymmetric_counter_regression_is_rejected(tmp_path: Path) -> None:
    """Queries advancing while hits regress is still a restart; the guard must catch it."""
    before = _snapshot(tmp_path, "b.prom", 1000.0, 200.0)
    after = _snapshot(tmp_path, "a.prom", 1200.0, 5.0)
    with pytest.raises(PrefixCacheError, match="backwards"):
        scrape_prefix_cache(
            metrics_before=before,
            metrics_after=after,
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_empty_query_window_is_rejected(tmp_path: Path) -> None:
    """No traffic between the snapshots is a 0/0 rate — no run to measure."""
    before = _snapshot(tmp_path, "b.prom", 1000.0, 200.0)
    with pytest.raises(PrefixCacheError, match="no prefix cache queries"):
        scrape_prefix_cache(
            metrics_before=before,
            metrics_after=before,
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_hits_exceeding_queries_is_rejected(tmp_path: Path) -> None:
    """Hits above queries over the window is impossible for a healthy series."""
    before = _snapshot(tmp_path, "b.prom", 1000.0, 200.0)
    after = _snapshot(tmp_path, "a.prom", 1100.0, 400.0)
    with pytest.raises(PrefixCacheError, match="exceed queries"):
        scrape_prefix_cache(
            metrics_before=before,
            metrics_after=after,
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_missing_model_selector_is_rejected(tmp_path: Path) -> None:
    """A blank model_id with no --model would sum every model's series; reject it."""
    blank = tmp_path / "blank.json"
    blank.write_text(json.dumps({"model_id": "", "completed": 5}))
    with pytest.raises(PrefixCacheError, match="could not determine model"):
        scrape_prefix_cache(
            metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=blank,
            cache_state="cold",
        )


def test_stub_client_json_is_rejected(tmp_path: Path) -> None:
    """A truncated run leaves valid JSON with no model_id; reject it."""
    stub = tmp_path / "stub.json"
    stub.write_text("{}")
    with pytest.raises(PrefixCacheError, match="missing model_id"):
        scrape_prefix_cache(
            metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=stub,
            cache_state="cold",
            model=MODEL,
        )


def test_missing_completed_is_rejected(tmp_path: Path) -> None:
    """A result with model_id but no completed count is a truncated run."""
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"model_id": MODEL}))
    with pytest.raises(PrefixCacheError, match="completed no requests"):
        scrape_prefix_cache(
            metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=partial,
            cache_state="cold",
        )


def test_zero_completed_run_is_rejected(tmp_path: Path) -> None:
    """A run where every request failed measured nothing; reject completed == 0."""
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"model_id": MODEL, "completed": 0}))
    with pytest.raises(PrefixCacheError, match="completed no requests"):
        scrape_prefix_cache(
            metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=empty,
            cache_state="cold",
        )


def test_malformed_exposition_is_rejected(tmp_path: Path) -> None:
    """A snapshot the parser cannot read fails with a clear message, not a traceback."""
    corrupt = tmp_path / "corrupt.prom"
    corrupt.write_text("vllm:prefix_cache_queries{model_name= 1100.0\n")
    with pytest.raises(PrefixCacheError, match="cannot parse"):
        scrape_prefix_cache(
            metrics_before=corrupt,
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_missing_snapshot_file_is_rejected(tmp_path: Path) -> None:
    """A snapshot path that does not exist fails with a clear not-found message."""
    with pytest.raises(PrefixCacheError, match="not found"):
        scrape_prefix_cache(
            metrics_before=tmp_path / "nope.prom",
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=_result(tmp_path),
            cache_state="cold",
        )


def test_unreadable_result_raises_result_error(tmp_path: Path) -> None:
    """A malformed client JSON surfaces the shared reader's ResultError."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ResultError, match="cannot read"):
        scrape_prefix_cache(
            metrics_before=_snapshot(tmp_path, "b.prom", 1000.0, 200.0),
            metrics_after=_snapshot(tmp_path, "a.prom", 1100.0, 210.0),
            result=bad,
            cache_state="cold",
        )
