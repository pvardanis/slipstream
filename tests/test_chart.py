"""Tests for the chart module: the ceiling table's CSV/JSON artifacts and the plots.

Pin the durable data artifacts first — the CSV and JSON the chart writes beside the
disposable PNGs — then the plots render to non-empty PNG files under the Agg backend.
"""

import json
from pathlib import Path

from slipstream_bench.chart import rows_to_csv, rows_to_json, write_artifacts


def _row(
    *,
    max_num_seqs: int = 64,
    kv_cache_dtype: str = "fp8",
    prefix_caching: bool = True,
    prefix_share: int = 50,
    ceiling: int | None = 32,
    timeout: int = 0,
    other: int = 0,
) -> dict:
    return {
        "max_num_seqs": max_num_seqs,
        "kv_cache_dtype": kv_cache_dtype,
        "prefix_caching": prefix_caching,
        "prefix_share": prefix_share,
        "ceiling": ceiling,
        "failures": {"timeout": timeout, "other": other, "oom": None},
        "num_preemptions": None,
    }


def test_csv_header_names_every_column() -> None:
    """The header carries each keyed field plus the flattened failure cohorts."""
    header = rows_to_csv([_row()]).splitlines()[0]
    assert header == (
        "max_num_seqs,kv_cache_dtype,prefix_caching,prefix_share,"
        "ceiling,timeout,other,oom,num_preemptions"
    )


def test_csv_row_flattens_the_failure_cohorts_and_keys() -> None:
    """A row renders its keys, ceiling, and the timeout/other/oom cohorts in order."""
    body = rows_to_csv([_row(ceiling=32, timeout=2, other=1)]).splitlines()[1]
    assert body == "64,fp8,True,50,32,2,1,,"


def test_csv_renders_a_missing_ceiling_as_an_empty_cell() -> None:
    """A point that held no rung has no ceiling — an empty cell, not a zero rung."""
    body = rows_to_csv([_row(ceiling=None)]).splitlines()[1]
    assert body.split(",")[4] == ""


def test_csv_writes_one_line_per_row_after_the_header() -> None:
    """Every ceiling row becomes one CSV line beneath the single header."""
    rows = [_row(prefix_share=10), _row(prefix_share=50), _row(prefix_share=90)]
    assert len(rows_to_csv(rows).splitlines()) == 1 + len(rows)


def test_json_round_trips_the_aggregated_rows() -> None:
    """The JSON artifact keeps the rows' structure — the durable, re-readable table."""
    rows = [_row(prefix_share=10), _row(prefix_share=90, ceiling=None)]
    assert json.loads(rows_to_json(rows)) == rows


def test_json_renders_a_missing_ceiling_as_null() -> None:
    """A point that held no rung reads back as None, not a zero rung."""
    reloaded = json.loads(rows_to_json([_row(ceiling=None)]))
    assert reloaded[0]["ceiling"] is None


def _grid_rows() -> list[dict]:
    """A ragged run: caching-on spans shares {10,50,90}, caching-off pins share 0."""
    rows = []
    for mns in (64, 128):
        for kv in ("fp8", "fp16"):
            for share in (10, 50, 90):
                rows.append(
                    _row(max_num_seqs=mns, kv_cache_dtype=kv, prefix_share=share)
                )
            rows.append(
                _row(
                    max_num_seqs=mns,
                    kv_cache_dtype=kv,
                    prefix_caching=False,
                    prefix_share=0,
                )
            )
    return rows


def test_write_artifacts_writes_a_non_empty_csv_json_and_png(tmp_path: Path) -> None:
    """The chart writes the two data artifacts and the primary plot, none empty."""
    written = write_artifacts(_grid_rows(), tmp_path / "charts")
    for path in written.values():
        assert path.is_file() and path.stat().st_size > 0


def test_write_artifacts_creates_the_charts_directory(tmp_path: Path) -> None:
    """The charts subdir is made on the way out — the run dir need not pre-hold it."""
    charts_dir = tmp_path / "run" / "charts"
    write_artifacts(_grid_rows(), charts_dir)
    assert charts_dir.is_dir()


def test_write_artifacts_csv_matches_the_renderer(tmp_path: Path) -> None:
    """The written CSV is exactly what rows_to_csv renders — one source of truth."""
    rows = _grid_rows()
    written = write_artifacts(rows, tmp_path / "charts")
    assert written["csv"].read_text() == rows_to_csv(rows)
