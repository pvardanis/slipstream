"""Tests for the L0 baseline report: self-hosted vs commercial $/1M at SLO.

Covers the join/segment spine (prefix-cache records carry cache_state + the
segment keys; self-hosted cost joins by shared source; commercial joins by
segment), the segmentation per concurrency (request_rate proxy) and prefix-share
bucket with cold/warm labelled and p95/p99 both reported, deterministic ordering,
the markdown rendering, and the fail-fast guards on a missing segment key, an
unjoinable self-hosted or commercial arm, and an empty spine.
"""

import json
from pathlib import Path

import pytest

from slipstream_bench.report import (
    ReportError,
    build_report,
    load_records,
    render_markdown,
)


def _prefix_cache(
    *,
    source: str,
    cache_state: str = "cold",
    request_rate: object = 8.0,
    prefix_share: object = 90,
) -> dict:
    return {
        "source": source,
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "cache_state": cache_state,
        "request_rate": request_rate,
        "prefix_share": prefix_share,
        "completed": 100,
        "prefix_cache_queries": 4096,
        "prefix_cache_hits": 3600,
        "prefix_cache_hit_rate": 0.879,
        "client_metrics": {
            "request_throughput": 8.0,
            "request_goodput": 7.5,
            "p95_ttft_ms": 850.0,
            "p99_ttft_ms": 990.0,
            "p95_tpot_ms": 42.0,
            "p99_tpot_ms": 48.0,
        },
    }


def _self_hosted(
    *, source: str, price_in: float = 0.12, price_out: float = 0.36
) -> dict:
    return {
        "source": source,
        "cost_per_1m_input_usd": price_in,
        "cost_per_1m_output_usd": price_out,
    }


def _commercial(
    *,
    request_rate: object = 8.0,
    prefix_share: object = 90,
    price_in: float = 0.15,
    price_out: float = 0.60,
) -> dict:
    return {
        "source": "bench/results/commercial/pshare90_burst1.0.json",
        "request_rate": request_rate,
        "prefix_share": prefix_share,
        "cost_per_1m_input_usd": price_in,
        "cost_per_1m_output_usd": price_out,
        "api": "openai",
        "model": "gpt-4o-mini",
    }


def test_row_joins_the_three_arms_per_segment() -> None:
    """A cold run's row carries self-hosted and commercial $/1M and its SLO tail."""
    source = "bench/results/prefix-cache/cold_pshare90_burst1.0.json"
    rows = build_report(
        self_hosted_cost=[_self_hosted(source=source)],
        commercial_cost=[_commercial()],
        prefix_cache=[_prefix_cache(source=source)],
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["concurrency"] == 8.0
    assert row["prefix_share"] == 90
    assert row["cache_state"] == "cold"
    assert row["self_hosted_usd_per_1m"] == {"input": 0.12, "output": 0.36}
    assert row["commercial_usd_per_1m"]["input"] == 0.15
    assert row["commercial_usd_per_1m"]["output"] == 0.60
    assert row["commercial_usd_per_1m"]["api"] == "openai"
    assert row["slo"]["p95_ttft_ms"] == 850.0
    assert row["slo"]["p99_ttft_ms"] == 990.0
    assert row["slo"]["p95_tpot_ms"] == 42.0
    assert row["slo"]["p99_tpot_ms"] == 48.0
    assert row["slo"]["request_goodput"] == 7.5


def test_rows_sorted_by_concurrency_then_share_then_cold_before_warm() -> None:
    """Segments order deterministically; within a segment cold precedes warm."""
    warm_hi = "bench/results/prefix-cache/warm_pshare90_burst1.0.json"
    cold_hi = "bench/results/prefix-cache/cold_pshare90_burst1.0.json"
    cold_lo = "bench/results/prefix-cache/cold_pshare10_burst1.0.json"
    rows = build_report(
        self_hosted_cost=[
            _self_hosted(source=warm_hi),
            _self_hosted(source=cold_hi),
            _self_hosted(source=cold_lo),
        ],
        commercial_cost=[
            _commercial(prefix_share=90),
            _commercial(prefix_share=10),
        ],
        prefix_cache=[
            _prefix_cache(source=warm_hi, cache_state="warm", prefix_share=90),
            _prefix_cache(source=cold_hi, cache_state="cold", prefix_share=90),
            _prefix_cache(source=cold_lo, cache_state="cold", prefix_share=10),
        ],
    )

    assert [(r["prefix_share"], r["cache_state"]) for r in rows] == [
        (10, "cold"),
        (90, "cold"),
        (90, "warm"),
    ]


def test_missing_segment_key_is_rejected() -> None:
    """A spine record with no prefix_share cannot be segmented — fail fast."""
    source = "bench/results/prefix-cache/cold_pshareNONE.json"
    with pytest.raises(ReportError, match="prefix_share"):
        build_report(
            self_hosted_cost=[_self_hosted(source=source)],
            commercial_cost=[_commercial()],
            prefix_cache=[_prefix_cache(source=source, prefix_share=None)],
        )


def test_unjoinable_commercial_arm_is_rejected() -> None:
    """A segment with no commercial record is not a baseline — fail fast."""
    source = "bench/results/prefix-cache/cold_pshare90_burst1.0.json"
    with pytest.raises(ReportError, match="commercial"):
        build_report(
            self_hosted_cost=[_self_hosted(source=source)],
            commercial_cost=[_commercial(prefix_share=10)],
            prefix_cache=[_prefix_cache(source=source, prefix_share=90)],
        )


def test_unjoinable_self_hosted_arm_is_rejected() -> None:
    """A run with no self-hosted cost record cannot be priced — fail fast."""
    source = "bench/results/prefix-cache/cold_pshare90_burst1.0.json"
    with pytest.raises(ReportError, match="self-hosted"):
        build_report(
            self_hosted_cost=[_self_hosted(source="other.json")],
            commercial_cost=[_commercial()],
            prefix_cache=[_prefix_cache(source=source)],
        )


def test_empty_spine_is_rejected() -> None:
    """No prefix-cache records means nothing to report — fail fast, not empty."""
    with pytest.raises(ReportError, match="no prefix-cache"):
        build_report(self_hosted_cost=[], commercial_cost=[], prefix_cache=[])


def test_markdown_renders_a_labelled_segmented_table() -> None:
    """The table labels cold/warm and carries both self-hosted and commercial $/1M."""
    source = "bench/results/prefix-cache/cold_pshare90_burst1.0.json"
    rows = build_report(
        self_hosted_cost=[_self_hosted(source=source)],
        commercial_cost=[_commercial()],
        prefix_cache=[_prefix_cache(source=source)],
    )

    table = render_markdown(rows)
    lines = table.splitlines()
    # A GitHub-flavored table: header, separator, one row.
    assert lines[1].startswith("|") and set(lines[1]) <= set("|- ")
    assert len(lines) == 3
    header = lines[0]
    for column in ("concurrency", "prefix-share", "cache", "self-hosted", "commercial"):
        assert column in header
    assert "p95" in header and "p99" in header
    row = lines[2]
    assert "cold" in row
    assert "90" in row
    assert "0.12" in row and "0.36" in row
    assert "0.15" in row and "0.60" in row


def test_load_records_reads_a_json_array(tmp_path: Path) -> None:
    """A cost file is a JSON array; it loads as its list of records."""
    path = tmp_path / "cost.json"
    path.write_text(json.dumps([{"a": 1}, {"a": 2}]))

    assert load_records(path) == [{"a": 1}, {"a": 2}]


def test_load_records_wraps_a_single_object(tmp_path: Path) -> None:
    """A prefix-cache file is one object; it loads as a one-element list."""
    path = tmp_path / "cold.json"
    path.write_text(json.dumps({"cache_state": "cold"}))

    assert load_records(path) == [{"cache_state": "cold"}]


def test_load_records_rejects_malformed_json(tmp_path: Path) -> None:
    """A file that is not JSON fails fast rather than joining nothing."""
    path = tmp_path / "bad.json"
    path.write_text("{not json")

    with pytest.raises(ReportError, match="cannot read"):
        load_records(path)


def test_load_records_rejects_a_non_record_element(tmp_path: Path) -> None:
    """An array element that is not an object cannot be a record."""
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps([{"a": 1}, "nope"]))

    with pytest.raises(ReportError, match="not a JSON object"):
        load_records(path)
