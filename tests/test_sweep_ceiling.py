"""Tests for the knob-sweep ceiling aggregator: point-key parse, goodput, cohorts.

Covers the engine-knob point key parsed off the sweep's per-point subdir name
(mns{N}_kv{fp8|fp16}_pc{on|off}), the closed-loop ceiling read off the Tier-2
--max-concurrency ladder at the 95%-goodput SLO, the {timeout, oom, other} failure
cohorts classified from the client JSON's per-request errors (oom and the
num_preemptions soft-fail signal marked not-captured until the recipe collects
them, ADR-0009), and the fail-fast guards on a malformed subdir name.
"""

import json
from pathlib import Path

import pytest

from slipstream_bench.results import ResultError
from slipstream_bench.sweep_ceiling import (
    EnginePoint,
    LoadCell,
    SweepCeilingError,
    aggregate,
    classify_failures,
    get_ceiling,
    goodput_fraction,
    read_cell,
)


def _write_cell(tmp_path: Path, name: str = "cell.json", **extra: object) -> Path:
    record = {
        "max_concurrency": 64,
        "prefix_share": 50,
        "request_goodput": 7.5,
        "request_throughput": 8.0,
        "errors": ["", "", ""],
    }
    record.update(extra)
    path = tmp_path / name
    path.write_text(json.dumps(record))
    return path


def _cell(max_concurrency: int, fraction: float, *, prefix_share: int = 50) -> LoadCell:
    return LoadCell(
        max_concurrency=max_concurrency,
        prefix_share=prefix_share,
        goodput_fraction=fraction,
        failures={"timeout": 0, "other": 0, "oom": None},
    )


def _write_rung(
    point_dir: Path,
    *,
    share: int,
    cap: int,
    fraction: float,
    errors: list[str] | None = None,
) -> None:
    """Lay down one ladder-rung JSON under a point subdir, as the recipe would."""
    point_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "max_concurrency": cap,
        "prefix_share": share,
        "request_goodput": fraction,
        "request_throughput": 1.0,
        "errors": errors if errors is not None else [""],
    }
    (point_dir / f"pshare{share}_burst1.0_mc{cap}.json").write_text(json.dumps(record))


# The engine-knob point key parsed off one sweep subdir name.


def test_parses_max_num_seqs_kv_dtype_and_prefix_caching() -> None:
    """mns64_kvfp8_pcon -> (64, fp8, prefix caching on)."""
    point = EnginePoint.from_dirname("mns64_kvfp8_pcon")
    assert point == EnginePoint(
        max_num_seqs=64, kv_cache_dtype="fp8", prefix_caching=True
    )


def test_reads_fp16_counterfactual_dtype_and_caching_off() -> None:
    """mns256_kvfp16_pcoff -> (256, fp16, prefix caching off)."""
    point = EnginePoint.from_dirname("mns256_kvfp16_pcoff")
    assert point == EnginePoint(
        max_num_seqs=256, kv_cache_dtype="fp16", prefix_caching=False
    )


@pytest.mark.parametrize(
    "name",
    [
        "mns64_kvfp8",  # missing the pc segment
        "seqs64_kvfp8_pcon",  # wrong mns prefix
        "mns64_kvint8_pcon",  # dtype outside {fp8, fp16}
        "mns64_kvfp8_pcmaybe",  # pc outside {on, off}
        "mnsx_kvfp8_pcon",  # non-numeric max-num-seqs
        "",  # empty
    ],
)
def test_rejects_a_malformed_subdir_name(name: str) -> None:
    """A subdir that names no valid engine point fails fast, not silently."""
    with pytest.raises(SweepCeilingError, match="point"):
        EnginePoint.from_dirname(name)


# The fraction of completed requests meeting the SLO, the 95% ceiling test.


def test_goodput_is_goodput_over_throughput() -> None:
    """7.5 goodput / 8.0 throughput req/s = the 0.9375 fraction meeting the SLO."""
    fraction = goodput_fraction(
        {"request_goodput": 7.5, "request_throughput": 8.0}, Path("cell.json")
    )
    assert fraction == pytest.approx(0.9375)


def test_a_cell_that_completed_nothing_holds_no_goodput() -> None:
    """Zero throughput measured no requests, so the fraction is 0.0, not undefined."""
    fraction = goodput_fraction(
        {"request_goodput": 0.0, "request_throughput": 0.0}, Path("cell.json")
    )
    assert fraction == 0.0


@pytest.mark.parametrize("metric", ["request_goodput", "request_throughput"])
def test_rejects_a_missing_or_non_numeric_metric(metric: str) -> None:
    """A metric absent or null cannot be priced into a fraction — fail fast."""
    record = {"request_goodput": 7.5, "request_throughput": 8.0}
    record[metric] = None
    with pytest.raises(SweepCeilingError, match=metric):
        goodput_fraction(record, Path("cell.json"))


# The {timeout, oom, other} cohorts read off a cell's per-request errors.


def test_splits_timeout_from_other_by_the_error_string() -> None:
    """A deadline-shaped error is a timeout; any other non-empty error is other."""
    record = {
        "errors": [
            "",
            "asyncio.exceptions.TimeoutError",
            "",
            "connection reset",
            "",
        ]
    }
    cohorts = classify_failures(record, Path("cell.json"))
    assert cohorts["timeout"] == 1
    assert cohorts["other"] == 1


def test_all_requests_succeeded_is_zero_of_every_cohort() -> None:
    """An empty error per completed request means no failures to cohort."""
    cohorts = classify_failures({"errors": ["", "", ""]}, Path("cell.json"))
    assert cohorts["timeout"] == 0
    assert cohorts["other"] == 0


def test_oom_is_not_captured_from_the_client_json() -> None:
    """oom needs a pod OOMKilled event and engine-log scrape the recipe omits."""
    cohorts = classify_failures({"errors": ["boom"]}, Path("cell.json"))
    assert cohorts["oom"] is None


def test_failures_without_a_detailed_errors_array_fall_to_other() -> None:
    """A result missing --save-detailed errors still knows completed vs attempted."""
    record = {"num_prompts": 10, "completed": 8}
    cohorts = classify_failures(record, Path("cell.json"))
    assert cohorts["timeout"] == 0
    assert cohorts["other"] == 2


@pytest.mark.parametrize("key", ["num_prompts", "completed"])
def test_a_shortfall_count_that_is_missing_or_non_int_is_rejected(key: str) -> None:
    """A no-detail result with a bad prompt/completed count fails fast with context,
    not an uncaught TypeError past the CLI's exit-2 path."""
    record = {"num_prompts": 10, "completed": 8}
    record[key] = "10"
    with pytest.raises(SweepCeilingError, match=key):
        classify_failures(record, Path("cell.json"))


def test_completed_above_attempted_is_rejected_as_inconsistent() -> None:
    """More completed than attempted is corrupt counting — fail fast, not a
    clamped-to-zero shortfall that hides the inconsistency."""
    record = {"num_prompts": 8, "completed": 10}
    with pytest.raises(SweepCeilingError, match="completed"):
        classify_failures(record, Path("cell.json"))


def test_a_non_list_errors_field_is_rejected() -> None:
    """A malformed errors field cannot be cohorted — fail fast, not a silent 0."""
    with pytest.raises(SweepCeilingError, match="errors"):
        classify_failures({"errors": "boom"}, Path("cell.json"))


def test_a_non_string_errors_entry_is_rejected() -> None:
    """A non-string entry has no error text to classify — fail fast with context,
    not an uncaught AttributeError past the CLI's exit-2 path."""
    with pytest.raises(SweepCeilingError, match="errors"):
        classify_failures({"errors": ["", 123]}, Path("cell.json"))


# The highest --max-concurrency rung still holding goodput >= 95%.


def test_ceiling_is_the_highest_rung_above_the_floor() -> None:
    """Goodput holds through 32 then falls at 64, so the ceiling is 32."""
    cells = [
        _cell(8, 0.99),
        _cell(16, 0.97),
        _cell(32, 0.96),
        _cell(64, 0.80),
        _cell(128, 0.50),
    ]
    assert get_ceiling(cells) == 32


def test_a_rung_exactly_at_the_floor_holds() -> None:
    """95% is meeting the SLO, not missing it, so a 0.95 rung counts."""
    assert get_ceiling([_cell(8, 0.95), _cell(16, 0.94)]) == 8


def test_no_rung_meets_the_floor_is_no_ceiling() -> None:
    """Even the lowest offered load misses the SLO — the point has no ceiling."""
    assert get_ceiling([_cell(8, 0.80), _cell(16, 0.50)]) is None


def test_takes_the_highest_passing_rung_even_past_a_dip() -> None:
    """The definition is the highest rung above the floor, robust to a noisy dip."""
    cells = [_cell(8, 0.99), _cell(16, 0.90), _cell(32, 0.96)]
    assert get_ceiling(cells) == 32


def test_no_cells_is_no_ceiling() -> None:
    """A point with no ladder cells measured nothing — no ceiling, not a zero."""
    assert get_ceiling([]) is None


# One Tier-2 client JSON read into a ladder cell.


def test_reads_the_rung_share_goodput_and_cohorts(tmp_path: Path) -> None:
    """A closed-loop cell yields its cap, share, goodput fraction, and cohorts."""
    cell = read_cell(_write_cell(tmp_path))
    assert cell.max_concurrency == 64
    assert cell.prefix_share == 50
    assert cell.goodput_fraction == pytest.approx(0.9375)
    assert cell.failures == {"timeout": 0, "other": 0, "oom": None}


@pytest.mark.parametrize("key", ["max_concurrency", "prefix_share"])
def test_rejects_a_cell_missing_its_join_key(tmp_path: Path, key: str) -> None:
    """An open-loop or un-stamped cell has no ceiling axis — fail fast."""
    path = _write_cell(tmp_path, **{key: None})
    with pytest.raises(SweepCeilingError, match=key):
        read_cell(path)


@pytest.mark.parametrize("key", ["max_concurrency", "prefix_share"])
def test_rejects_a_boolean_join_key(tmp_path: Path, key: str) -> None:
    """bool is an int subclass, so a JSON true must not slip through as a 1 cap
    or share — the guard rejects it."""
    path = _write_cell(tmp_path, **{key: True})
    with pytest.raises(SweepCeilingError, match=key):
        read_cell(path)


def test_an_unreadable_file_raises_result_error(tmp_path: Path) -> None:
    """A missing result file surfaces as the shared reader's error."""
    with pytest.raises(ResultError):
        read_cell(tmp_path / "absent.json")


# The whole run directory folded into ceiling rows, one per point-and-share.


def test_one_row_per_point_and_share_with_the_ceiling(tmp_path: Path) -> None:
    """Each (point, share) group's highest holding rung becomes its ceiling."""
    point = tmp_path / "mns64_kvfp8_pcon"
    _write_rung(point, share=50, cap=32, fraction=0.98)
    _write_rung(point, share=50, cap=64, fraction=0.80)
    _write_rung(point, share=90, cap=32, fraction=0.99)

    rows = aggregate(tmp_path)

    assert rows == [
        {
            "max_num_seqs": 64,
            "kv_cache_dtype": "fp8",
            "prefix_caching": True,
            "prefix_share": 50,
            "ceiling": 32,
            "failures": {"timeout": 0, "other": 0, "oom": None},
            "num_preemptions": None,
        },
        {
            "max_num_seqs": 64,
            "kv_cache_dtype": "fp8",
            "prefix_caching": True,
            "prefix_share": 90,
            "ceiling": 32,
            "failures": {"timeout": 0, "other": 0, "oom": None},
            "num_preemptions": None,
        },
    ]


def test_sums_failure_cohorts_across_a_group_ladder(tmp_path: Path) -> None:
    """A group's cohort counts add up across its ladder rungs."""
    point = tmp_path / "mns16_kvfp16_pcoff"
    _write_rung(point, share=10, cap=8, fraction=0.99, errors=["TimeoutError", ""])
    _write_rung(point, share=10, cap=16, fraction=0.90, errors=["broke", "broke"])

    (row,) = aggregate(tmp_path)

    assert row["failures"] == {"timeout": 1, "other": 2, "oom": None}
    assert row["ceiling"] == 8


def test_rows_sort_by_point_then_share(tmp_path: Path) -> None:
    """Rows order by max-num-seqs, kv-dtype, prefix-caching, then share."""
    _write_rung(tmp_path / "mns256_kvfp8_pcon", share=10, cap=8, fraction=0.99)
    _write_rung(tmp_path / "mns16_kvfp8_pcon", share=10, cap=8, fraction=0.99)

    rows = aggregate(tmp_path)

    assert [row["max_num_seqs"] for row in rows] == [16, 256]


def test_ignores_a_non_point_subdir_and_the_ledger(tmp_path: Path) -> None:
    """The charts/ dir and predicted-ceilings.tsv are not engine points."""
    _write_rung(tmp_path / "mns32_kvfp8_pcon", share=50, cap=8, fraction=0.99)
    (tmp_path / "charts").mkdir()
    (tmp_path / "predicted-ceilings.tsv").write_text("point\tpredicted\n")

    rows = aggregate(tmp_path)

    assert len(rows) == 1
    assert rows[0]["max_num_seqs"] == 32


def test_a_run_with_no_point_subdirs_is_rejected(tmp_path: Path) -> None:
    """An empty run measured nothing — fail fast rather than emit no rows."""
    with pytest.raises(SweepCeilingError, match="no knob-sweep points"):
        aggregate(tmp_path)


def test_a_point_subdir_with_no_rungs_is_rejected(tmp_path: Path) -> None:
    """A point whose sweep collected no rungs measured nothing — fail fast rather
    than silently drop the point from the table."""
    (tmp_path / "mns64_kvfp8_pcon").mkdir()
    with pytest.raises(SweepCeilingError, match="no ladder"):
        aggregate(tmp_path)
