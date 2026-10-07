"""The plotters' in-memory PNG renders: draw a run's two charts straight to PNG bytes.

The render task uploads each plot to S3 and embeds it inline on the run page, so it needs the
chart as bytes, not a file on a worker disk. These exercise the bytes path (plot_ceilings_png,
plot_cliffs_png) against sample rows: each returns a non-empty PNG, and an empty table raises
rather than drawing a blank figure — the same guard write_artifacts holds for the on-disk path.
"""

import matplotlib.pyplot as plt
import pytest

from slipstream_bench.report.aggregation import CeilingRow, RungRow
from slipstream_bench.report.plotters import plot_ceilings_png, plot_cliffs_png

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
) -> CeilingRow:
    return {
        "max_num_seqs": max_num_seqs,
        "kv_cache_dtype": kv_cache_dtype,
        "prefix_caching": prefix_caching,
        "prefix_share": prefix_share,
        "ceiling": ceiling,
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
) -> RungRow:
    return {
        "max_num_seqs": max_num_seqs,
        "kv_cache_dtype": kv_cache_dtype,
        "prefix_caching": prefix_caching,
        "prefix_share": prefix_share,
        "max_concurrency": max_concurrency,
        "goodput_fraction": goodput_fraction,
    }


def test_plot_ceilings_png_renders_a_png() -> None:
    png = plot_ceilings_png([_row(), _row(prefix_share=10)])

    assert png.startswith(_PNG_MAGIC)


def test_plot_cliffs_png_renders_a_png() -> None:
    png = plot_cliffs_png([_rung(max_concurrency=32), _rung(max_concurrency=64)])

    assert png.startswith(_PNG_MAGIC)


def test_rendering_a_plot_leaves_no_open_figure() -> None:
    # _figure_png closes each grid's figure after encoding, so a long-lived worker that renders
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


def test_plot_cliffs_png_rejects_an_empty_table() -> None:
    with pytest.raises(ValueError, match="empty cliff"):
        plot_cliffs_png([])
