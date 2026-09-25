"""Tests for the chart module: the ceiling table's Markdown/JSON artifacts and plots.

Pin the durable data artifacts first — the Markdown and JSON the chart writes beside
the disposable PNGs — then the facet-derivation helpers and the plots that render to
non-empty PNG files under the Agg backend.
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import pytest

from slipstream_bench.report.chart import (
    _ceiling_frame,
    _cliff_frame,
    _condition_label,
    _condition_order,
    _plot_cliffs,
    _point_label,
    _point_order,
    _share_label,
    rows_to_json,
    rows_to_markdown,
    rungs_to_json,
    rungs_to_markdown,
    write_artifacts,
)
from slipstream_bench.sweep.aggregation import _GOODPUT_FLOOR, CeilingRow, RungRow


def _row(
    *,
    max_num_seqs: int = 64,
    kv_cache_dtype: str = "fp8",
    prefix_caching: bool = True,
    prefix_share: int = 50,
    ceiling: int | None = 32,
    timeout: int = 0,
    other: int = 0,
) -> CeilingRow:
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


def test_condition_label_folds_caching_and_share_into_one_facet_name() -> None:
    """A caching-on row names its share; a caching-off row names its n/a baseline."""
    assert _condition_label(_row(prefix_caching=True, prefix_share=90)) == (
        "caching on · share 90"
    )
    assert _condition_label(_row(prefix_caching=False, prefix_share=0)) == (
        "caching off · share n/a"
    )


def test_ceiling_frame_folds_the_condition_and_keeps_a_missing_ceiling() -> None:
    """The frame carries the combined condition and preserves a None ceiling as a gap."""
    frame = _ceiling_frame(
        [
            _row(prefix_caching=True, prefix_share=50, ceiling=32),
            _row(prefix_caching=False, prefix_share=0, ceiling=None),
        ]
    )
    assert list(frame["condition"]) == [
        "caching on · share 50",
        "caching off · share n/a",
    ]
    assert frame["ceiling"].isna().tolist() == [False, True]


def test_condition_order_sorts_shares_numerically_behind_the_off_baseline() -> None:
    """Facets order the caching-off baseline first, then swept shares by number."""
    frame = _ceiling_frame(
        [
            _row(prefix_caching=True, prefix_share=90),
            _row(prefix_caching=True, prefix_share=100),
            _row(prefix_caching=True, prefix_share=9),
            _row(prefix_caching=False, prefix_share=0),
        ]
    )
    assert _condition_order(frame) == [
        "caching off · share n/a",
        "caching on · share 9",
        "caching on · share 90",
        "caching on · share 100",
    ]


def test_condition_order_omits_the_baseline_when_no_caching_off_row_is_present() -> (
    None
):
    """A run with only caching-on points carries no baseline facet."""
    frame = _ceiling_frame([_row(prefix_caching=True, prefix_share=50)])
    assert _condition_order(frame) == ["caching on · share 50"]


def _rung(
    *,
    max_num_seqs: int = 64,
    kv_cache_dtype: str = "fp8",
    prefix_caching: bool = True,
    prefix_share: int = 50,
    max_concurrency: int = 32,
    goodput_fraction: float = 0.98,
) -> RungRow:
    return {
        "max_num_seqs": max_num_seqs,
        "kv_cache_dtype": kv_cache_dtype,
        "prefix_caching": prefix_caching,
        "prefix_share": prefix_share,
        "max_concurrency": max_concurrency,
        "goodput_fraction": goodput_fraction,
    }


def test_rungs_markdown_header_and_separator_name_every_column() -> None:
    """The diagnostic table heads the point knobs, the offered cap, and the goodput."""
    header, separator = rungs_to_markdown([_rung()]).splitlines()[:2]
    assert header == (
        "| max_num_seqs | kv_cache_dtype | prefix_caching | prefix_share | "
        "max_concurrency | goodput_fraction |"
    )
    assert separator == "| " + " | ".join(["---"] * 6) + " |"


def test_rungs_markdown_renders_the_goodput_fraction_to_three_decimals() -> None:
    """The human view rounds the messy goodput/throughput ratio; the JSON keeps it."""
    body = rungs_to_markdown([_rung(goodput_fraction=0.937541)]).splitlines()[2]
    assert body == "| 64 | fp8 | True | 50 | 32 | 0.938 |"


def test_rungs_json_round_trips_the_full_precision_fraction() -> None:
    """The JSON artifact keeps the rung rows verbatim — the durable, re-readable cliff."""
    rungs = [_rung(goodput_fraction=0.937541), _rung(max_concurrency=64)]
    assert json.loads(rungs_to_json(rungs)) == rungs


def test_point_label_names_the_engine_deploy_the_facet_stands_for() -> None:
    """A rung's facet is its engine point: the batch cap, KV dtype, and caching."""
    assert _point_label(_rung(max_num_seqs=128, kv_cache_dtype="fp16")) == (
        "mns128 · fp16 · caching on"
    )
    assert _point_label(_rung(prefix_caching=False)) == "mns64 · fp8 · caching off"


def test_cliff_frame_folds_the_point_and_share_labels_and_keeps_goodput() -> None:
    """The frame carries the facet point, the hue share, and the rung's goodput."""
    frame = _cliff_frame(
        [
            _rung(max_concurrency=32, goodput_fraction=0.98),
            _rung(prefix_caching=False, prefix_share=0, goodput_fraction=0.80),
        ]
    )
    assert list(frame["point"]) == [
        "mns64 · fp8 · caching on",
        "mns64 · fp8 · caching off",
    ]
    assert list(frame["prefix_share"]) == ["50", "n/a"]
    assert list(frame["goodput_fraction"]) == [0.98, 0.80]


def test_point_order_keeps_the_aggregators_point_key_order() -> None:
    """Facets follow the order aggregate_rungs already sorted the points into."""
    frame = _cliff_frame(
        [
            _rung(max_num_seqs=64, kv_cache_dtype="fp8"),
            _rung(max_num_seqs=64, kv_cache_dtype="fp8", max_concurrency=64),
            _rung(max_num_seqs=128, kv_cache_dtype="fp16"),
        ]
    )
    assert _point_order(frame) == [
        "mns64 · fp8 · caching on",
        "mns128 · fp16 · caching on",
    ]


def _grid_rungs() -> list[RungRow]:
    """A ragged run's rungs: caching-on spans shares {10,50,90}, off pins share 0.

    Each point holds a two-rung ladder with a cliff — the high rung dips below the
    floor — so the diagnostic plot exercises a real drop, not a flat line.
    """
    rungs = []
    for mns in (64, 128):
        for kv in ("fp8", "fp16"):
            for share in (10, 50, 90):
                for cap, frac in ((8, 0.99), (64, 0.80)):
                    rungs.append(
                        _rung(
                            max_num_seqs=mns,
                            kv_cache_dtype=kv,
                            prefix_share=share,
                            max_concurrency=cap,
                            goodput_fraction=frac,
                        )
                    )
            for cap, frac in ((8, 0.99), (64, 0.80)):
                rungs.append(
                    _rung(
                        max_num_seqs=mns,
                        kv_cache_dtype=kv,
                        prefix_caching=False,
                        prefix_share=0,
                        max_concurrency=cap,
                        goodput_fraction=frac,
                    )
                )
    return rungs


def _grid_rows() -> list[CeilingRow]:
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


def test_write_artifacts_writes_both_tables_and_both_plots_none_empty(
    tmp_path: Path,
) -> None:
    """The chart writes the ceiling and cliff data artifacts and both plots, none empty."""
    written = write_artifacts(_grid_rows(), _grid_rungs(), tmp_path / "charts")
    assert set(written) == {
        "markdown",
        "json",
        "png",
        "rungs_markdown",
        "rungs_json",
        "rungs_png",
    }
    for path in written.values():
        assert path.is_file() and path.stat().st_size > 0


def test_write_artifacts_creates_the_charts_directory(tmp_path: Path) -> None:
    """The charts subdir is made on the way out — the run dir need not pre-hold it."""
    charts_dir = tmp_path / "run" / "charts"
    write_artifacts(_grid_rows(), _grid_rungs(), charts_dir)
    assert charts_dir.is_dir()


def test_write_artifacts_tables_match_their_renderers(tmp_path: Path) -> None:
    """The written tables are exactly what the renderers emit — one source each."""
    rows, rungs = _grid_rows(), _grid_rungs()
    written = write_artifacts(rows, rungs, tmp_path / "charts")
    assert written["markdown"].read_text() == rows_to_markdown(rows)
    assert written["json"].read_text() == rows_to_json(rows)
    assert written["rungs_markdown"].read_text() == rungs_to_markdown(rungs)
    assert written["rungs_json"].read_text() == rungs_to_json(rungs)


def test_write_artifacts_rejects_an_empty_ceiling_table(tmp_path: Path) -> None:
    """An empty run holds no ceiling and must not write a header-only table."""
    with pytest.raises(ValueError, match="empty ceiling table"):
        write_artifacts([], [], tmp_path / "charts")
    assert not (tmp_path / "charts").exists()


def test_write_artifacts_rejects_empty_rungs(tmp_path: Path) -> None:
    """Non-empty ceilings with no rungs must not write a header-only cliff table."""
    with pytest.raises(ValueError, match="empty cliff table"):
        write_artifacts(_grid_rows(), [], tmp_path / "charts")
    assert not (tmp_path / "charts").exists()


def test_plot_cliffs_refline_sits_at_the_aggregators_floor() -> None:
    """The crimson reference line tracks the aggregator's floor, not a copied number."""
    grid = _plot_cliffs(_grid_rungs())
    try:
        reflines = [
            line
            for ax in grid.axes.flat
            for line in ax.lines
            if line.get_linestyle() == "--"
        ]
        assert reflines
        for line in reflines:
            assert list(line.get_ydata()) == [_GOODPUT_FLOOR, _GOODPUT_FLOOR]
    finally:
        plt.close(grid.figure)
