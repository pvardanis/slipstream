"""Tests for the chart module: the ceiling table's Markdown/JSON artifacts and plots.

Pin the durable data artifacts first — the Markdown and JSON the chart writes beside
the disposable PNGs — then the facet-derivation helpers and the plots that render to
non-empty PNG files under the Agg backend.
"""

import json
from pathlib import Path

import pytest

from slipstream_bench.chart import (
    _ceiling_frame,
    _share_label,
    _share_order,
    rows_to_json,
    rows_to_markdown,
    write_artifacts,
)


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


def test_markdown_header_and_separator_name_every_column() -> None:
    """The header carries each keyed field plus the flattened cohorts, then a rule."""
    header, separator = rows_to_markdown([_row()]).splitlines()[:2]
    assert header == (
        "| max_num_seqs | kv_cache_dtype | prefix_caching | prefix_share | "
        "ceiling | timeout | other | oom | num_preemptions |"
    )
    assert separator == "| " + " | ".join(["---"] * 9) + " |"


def test_markdown_row_flattens_the_failure_cohorts_and_keys() -> None:
    """A row renders its keys, ceiling, and the timeout/other/oom cohorts in order."""
    body = rows_to_markdown([_row(ceiling=32, timeout=2, other=1)]).splitlines()[2]
    assert body == "| 64 | fp8 | True | 50 | 32 | 2 | 1 |  |  |"


def test_markdown_renders_a_missing_ceiling_as_a_blank_cell() -> None:
    """A point that held no rung has no ceiling — a blank cell, not a zero rung."""
    body = rows_to_markdown([_row(ceiling=None)]).splitlines()[2]
    assert body.split(" | ")[4] == ""


def test_markdown_writes_one_line_per_row_after_the_header_and_rule() -> None:
    """Every ceiling row becomes one line beneath the single header and separator."""
    rows = [_row(prefix_share=10), _row(prefix_share=50), _row(prefix_share=90)]
    assert len(rows_to_markdown(rows).splitlines()) == 2 + len(rows)


def test_json_round_trips_the_aggregated_rows() -> None:
    """The JSON artifact keeps the rows' structure — the durable, re-readable table."""
    rows = [_row(prefix_share=10), _row(prefix_share=90, ceiling=None)]
    assert json.loads(rows_to_json(rows)) == rows


def test_json_renders_a_missing_ceiling_as_null() -> None:
    """A point that held no rung reads back as None, not a zero rung."""
    reloaded = json.loads(rows_to_json([_row(ceiling=None)]))
    assert reloaded[0]["ceiling"] is None


def test_share_label_reads_the_swept_share_when_caching_is_on() -> None:
    """A caching-on row labels its facet column with the swept share value."""
    assert _share_label(_row(prefix_caching=True, prefix_share=90)) == "90"


def test_share_label_is_n_a_when_caching_is_off() -> None:
    """A caching-off row reused no prefix KV, so its share column is a definitional n/a."""
    assert _share_label(_row(prefix_caching=False, prefix_share=0)) == "n/a"


def test_ceiling_frame_maps_caching_to_on_off_and_keeps_a_missing_ceiling() -> None:
    """The frame carries on/off caching labels and preserves a None ceiling as a gap."""
    frame = _ceiling_frame(
        [
            _row(prefix_caching=True, prefix_share=50, ceiling=32),
            _row(prefix_caching=False, prefix_share=0, ceiling=None),
        ]
    )
    assert list(frame["prefix_caching"]) == ["on", "off"]
    assert list(frame["prefix_share"]) == ["50", "n/a"]
    assert frame["ceiling"].isna().tolist() == [False, True]


def test_share_order_sorts_shares_numerically_behind_the_n_a_baseline() -> None:
    """Facet columns order the n/a baseline first, then swept shares by number not text."""
    frame = _ceiling_frame(
        [
            _row(prefix_caching=True, prefix_share=90),
            _row(prefix_caching=True, prefix_share=100),
            _row(prefix_caching=True, prefix_share=9),
            _row(prefix_caching=False, prefix_share=0),
        ]
    )
    assert _share_order(frame) == ["n/a", "9", "90", "100"]


def test_share_order_omits_the_baseline_when_no_caching_off_row_is_present() -> None:
    """A run with only caching-on points carries no n/a column."""
    frame = _ceiling_frame([_row(prefix_caching=True, prefix_share=50)])
    assert _share_order(frame) == ["50"]


def _grid_rows() -> list[dict]:
    """A ragged run: caching-on spans shares {10,50,90}, caching-off pins share 0.

    One caching-on point holds no rung (ceiling None), so the frame and plot exercise
    the documented missing-ceiling gap, not only the CSV/JSON blank cell.
    """
    rows = []
    for mns in (64, 128):
        for kv in ("fp8", "fp16"):
            for share in (10, 50, 90):
                ceiling = None if (mns, kv, share) == (128, "fp16", 90) else 32
                rows.append(
                    _row(
                        max_num_seqs=mns,
                        kv_cache_dtype=kv,
                        prefix_share=share,
                        ceiling=ceiling,
                    )
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


def test_write_artifacts_writes_a_non_empty_markdown_json_and_png(
    tmp_path: Path,
) -> None:
    """The chart writes the two data artifacts and the primary plot, none empty."""
    written = write_artifacts(_grid_rows(), tmp_path / "charts")
    for path in written.values():
        assert path.is_file() and path.stat().st_size > 0


def test_write_artifacts_creates_the_charts_directory(tmp_path: Path) -> None:
    """The charts subdir is made on the way out — the run dir need not pre-hold it."""
    charts_dir = tmp_path / "run" / "charts"
    write_artifacts(_grid_rows(), charts_dir)
    assert charts_dir.is_dir()


def test_write_artifacts_tables_match_their_renderers(tmp_path: Path) -> None:
    """The written Markdown and JSON are exactly what the renderers emit — one source."""
    rows = _grid_rows()
    written = write_artifacts(rows, tmp_path / "charts")
    assert written["markdown"].read_text() == rows_to_markdown(rows)
    assert written["json"].read_text() == rows_to_json(rows)


def test_write_artifacts_rejects_an_empty_ceiling_table(tmp_path: Path) -> None:
    """An empty run holds no ceiling and must not write a header-only table."""
    with pytest.raises(ValueError, match="empty ceiling table"):
        write_artifacts([], tmp_path / "charts")
    assert not (tmp_path / "charts").exists()
