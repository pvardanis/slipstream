"""The plotters' in-memory PNG renders: draw a run's two charts straight to PNG bytes.

The render task uploads each plot to S3 and embeds it inline on the run page, so it needs the
chart as bytes, not a file on a worker disk. These exercise the bytes path (plot_ceilings_png,
plot_cliffs_png) against sample rows: each returns a non-empty PNG, and an empty table raises
rather than drawing a blank figure — the same guard write_artifacts holds for the on-disk path.
"""

import matplotlib.pyplot as plt
import pytest

from slipstream_bench.report.aggregation import CeilingRow, RungRow
from slipstream_bench.report.plotters import (
    _plot_cliffs,
    plot_ceilings_png,
    plot_cliffs_png,
)

# A PNG file leads with this 8-byte signature; asserting it pins the bytes are a real PNG, not
# an empty buffer or a stray format.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _row(
    *,
    max_num_seqs: int = 64,
    kv_cache_dtype: str = "fp8",
    prefix_caching: bool = True,
    prefix_share: int = 50,
    ceiling: int | None = 32,
    p95_ttft_ms: float | None = 850.0,
    p95_tpot_ms: float | None = 42.0,
    output_throughput: float | None = 1234.5,
) -> CeilingRow:
    return {
        "max_num_seqs": max_num_seqs,
        "kv_cache_dtype": kv_cache_dtype,
        "prefix_caching": prefix_caching,
        "prefix_share": prefix_share,
        "ceiling": ceiling,
        "p95_ttft_ms": p95_ttft_ms,
        "p95_tpot_ms": p95_tpot_ms,
        "output_throughput": output_throughput,
        "failures": {"timeout": 0, "other": 0, "oom": None},
        "num_preemptions": None,
    }


def _rung(
    *,
    max_num_seqs: int = 64,
    kv_cache_dtype: str = "fp8",
    prefix_caching: bool = True,
    prefix_share: int = 50,
    max_concurrency: int = 32,
    goodput_fraction: float = 0.98,
    p95_ttft_ms: float = 850.0,
    p95_tpot_ms: float = 42.0,
    output_throughput: float = 1234.5,
) -> RungRow:
    return {
        "max_num_seqs": max_num_seqs,
        "kv_cache_dtype": kv_cache_dtype,
        "prefix_caching": prefix_caching,
        "prefix_share": prefix_share,
        "max_concurrency": max_concurrency,
        "goodput_fraction": goodput_fraction,
        "p95_ttft_ms": p95_ttft_ms,
        "p95_tpot_ms": p95_tpot_ms,
        "output_throughput": output_throughput,
    }


def test_plot_ceilings_png_renders_a_png() -> None:
    png = plot_ceilings_png([_row(), _row(prefix_share=10)])

    assert png.startswith(_PNG_MAGIC)


def test_plot_cliffs_png_renders_a_png() -> None:
    png = plot_cliffs_png([_rung(max_concurrency=32), _rung(max_concurrency=64)])

    assert png.startswith(_PNG_MAGIC)


def test_rendering_a_plot_leaves_no_open_figure() -> None:
    # _get_figure_png_bytes closes each grid's figure after encoding, so a long-lived worker that renders
    # every run does not leak a matplotlib figure per plot.
    plt.close("all")

    plot_ceilings_png([_row(), _row(prefix_share=10)])
    plot_cliffs_png([_rung(max_concurrency=32), _rung(max_concurrency=64)])

    assert plt.get_fignums() == []


def test_plot_ceilings_png_rejects_an_empty_table() -> None:
    # An empty ceiling table holds no ceiling to draw, so it raises rather than rendering a
    # blank figure — the same guard the on-disk write_artifacts path holds.
    with pytest.raises(ValueError, match="empty ceiling"):
        plot_ceilings_png([])


def test_plot_ceilings_png_renders_when_some_points_held_no_ceiling() -> None:
    # A point whose every rung fell below the SLO carries a null ceiling: it plots as a
    # gap, not a zero. The chart still renders off the points that did hold a ceiling —
    # a null-ceiling row must not drop the whole y-column and crash the render.
    png = plot_ceilings_png([_row(ceiling=32), _row(prefix_share=10, ceiling=None)])

    assert png.startswith(_PNG_MAGIC)


def test_plot_ceilings_png_rejects_a_table_with_no_ceiling_anywhere() -> None:
    # When no point in the run held a ceiling, there is nothing to draw, so it raises
    # rather than rendering a blank figure — a sibling of the empty-table guard, naming
    # the all-null cause. Match that message, not the shared prefix, to pin this branch.
    with pytest.raises(ValueError, match="no point held a ceiling"):
        plot_ceilings_png([_row(ceiling=None), _row(prefix_share=10, ceiling=None)])


def test_plot_cliffs_png_rejects_an_empty_table() -> None:
    with pytest.raises(ValueError, match="empty cliff"):
        plot_cliffs_png([])


def test_cliff_markers_carry_their_metric_labels() -> None:
    # Each rung marker is annotated with its own SLO gates and token rate, so the cliff
    # reads not only where a rung fell but which gate bit (ttft prefill / tpot decode) and
    # at what capacity. The label text is pinned, not the pixels.
    grid = _plot_cliffs(
        [_rung(p95_ttft_ms=850.0, p95_tpot_ms=42.0, output_throughput=1235.0)]
    )
    labels = [text.get_text() for ax in grid.axes.flat for text in ax.texts]
    plt.close(grid.figure)

    assert any(
        "ttft 850" in label and "tpot 42" in label and "1235 t/s" in label
        for label in labels
    )


def test_cliff_labels_only_the_ceiling_and_the_rungs_that_fell() -> None:
    # A passing rung below the ceiling reads the same flat gates as the ceiling, so it is
    # left unlabeled to spare the facet a wall of redundant numbers. The ceiling (highest
    # passing rung) and every failing rung keep their labels — those are the ones that tell
    # which gate bit on the fall.
    grid = _plot_cliffs(
        [
            _rung(max_concurrency=1, goodput_fraction=0.99, p95_ttft_ms=100.0),
            _rung(max_concurrency=2, goodput_fraction=0.97, p95_ttft_ms=200.0),
            _rung(max_concurrency=4, goodput_fraction=0.50, p95_ttft_ms=300.0),
        ]
    )
    labels = [text.get_text() for ax in grid.axes.flat for text in ax.texts]
    plt.close(grid.figure)

    assert not any("ttft 100" in label for label in labels)
    assert any("ttft 200" in label for label in labels)
    assert any("ttft 300" in label for label in labels)
