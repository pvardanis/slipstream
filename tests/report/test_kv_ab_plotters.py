"""Tests for the kv-ab chart: three panels comparing the arms across the concurrency ladder.

The A/B's render uploads the chart to S3 and embeds it on the flow run page, so the plotter
draws it straight to PNG bytes. Each panel plots one metric against ``--max-concurrency``,
a line per arm through its runs' medians, and the goodput panel draws the floor the
ceiling is read at.
"""

import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.collections import PathCollection
from matplotlib.colors import to_hex

from slipstream_bench.report.kv_ab_aggregation import KvAbRung
from slipstream_bench.report.kv_ab_plotters import _plot_kv_ab, plot_kv_ab_png
from tests.report.kv_ab_records import build_rung

# A PNG file leads with this 8-byte signature.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _rungs() -> list[KvAbRung]:
    return [
        build_rung(arm, run, cap)
        for arm in ("off", "on")
        for run in (1, 2, 3)
        for cap in (1, 2, 4)
    ]


def test_the_chart_renders_to_png_bytes() -> None:
    assert plot_kv_ab_png(_rungs()).startswith(_PNG_MAGIC)


def test_an_empty_run_is_refused_rather_than_drawn_blank() -> None:
    with pytest.raises(ValueError, match="no rungs"):
        plot_kv_ab_png([])


def test_the_panels_plot_hit_rate_ttft_and_goodput_against_the_ladder() -> None:
    figure = _plot_kv_ab(_rungs())
    try:
        axes = figure.axes
        assert [ax.get_ylabel() for ax in axes] == [
            "combined hit rate (GPU + CPU tier)",
            "p95 TTFT (ms)",
            "goodput fraction (met SLO / completed)",
        ]
        assert {ax.get_xlabel() for ax in axes} == {"--max-concurrency"}
        # The goodput panel's dashed floor sits at the 0.95 the ceiling is read at.
        floors = [
            float(np.asarray(line.get_ydata())[0])
            for line in axes[2].get_lines()
            if line.get_linestyle() == "--"
        ]
        assert floors == [0.95]
        for ax in axes:
            legend = ax.get_legend()
            assert legend is not None
            assert {text.get_text() for text in legend.get_texts()} >= {"off", "on"}
    finally:
        plt.close(figure)


def test_each_runs_dot_is_nudged_alike_at_every_rung_of_the_log_axis() -> None:
    # Three runs reading alike on a 1, 2, 64 ladder: on the log-2 axis the closest rungs
    # sit one unit apart, so every rung's dots spread within the same +/-0.08 log-2 band,
    # the high rung's as visibly as the low ones'. Seaborn jitters off numpy's global
    # generator, seeded here so the spread is fixed.
    np.random.seed(0)
    rungs = [build_rung("on", run, cap) for run in (1, 2, 3) for cap in (1, 2, 64)]
    figure = _plot_kv_ab(rungs)
    try:
        dots = np.concatenate(
            [
                np.asarray(c.get_offsets())[:, 0]
                for c in figure.axes[0].collections
                if isinstance(c, PathCollection)
            ]
        )
        for cap in (1, 2, 64):
            nudges = np.log2(dots[np.isclose(dots, cap, rtol=0.1)]) - np.log2(cap)
            assert len(nudges) == 3
            assert np.all(np.abs(nudges) <= 0.08)
            assert np.ptp(nudges) > 0.02
    finally:
        plt.close(figure)


@pytest.mark.parametrize("ladder", [(4,), (1, 2, 4)])
def test_the_axis_spans_the_ladder_and_half_an_octave_either_side(
    ladder: tuple[int, ...],
) -> None:
    # A one-rung ladder gives matplotlib no span to fit but the dots' own nudge, so
    # without fixed limits the panel zooms onto that nudge and pushes the runs to its
    # edges. Half an octave either side keeps a lone rung's dots as close as a ladder's.
    rungs = [
        build_rung(arm, run, cap)
        for arm in ("off", "on")
        for run in (1, 2)
        for cap in ladder
    ]
    figure = _plot_kv_ab(rungs)
    try:
        for ax in figure.axes:
            low, high = np.log2(ax.get_xlim())
            assert low == pytest.approx(np.log2(ladder[0]) - 0.5)
            assert high == pytest.approx(np.log2(ladder[-1]) + 0.5)
    finally:
        plt.close(figure)


def test_each_arms_line_runs_through_its_runs_median_not_their_mean() -> None:
    # One skewed run per arm pulls the mean well off the median: off 600/610/900 has
    # median 610 (mean 703.3), on 300/310/500 has median 310 (mean 370).
    ttfts = {"off": (600.0, 610.0, 900.0), "on": (300.0, 310.0, 500.0)}
    rungs = [
        build_rung(arm, run, 1, p95_ttft_ms=ttft)
        for arm, runs in ttfts.items()
        for run, ttft in enumerate(runs, start=1)
    ]
    figure = _plot_kv_ab(rungs)
    try:
        lines = {
            to_hex(line.get_color()): float(np.asarray(line.get_ydata())[0])
            for line in figure.axes[1].get_lines()
            if len(np.asarray(line.get_ydata()))  # skip the legend's empty handles
        }
        assert lines == {"#7f7f7f": 610.0, "#1f77b4": 310.0}
    finally:
        plt.close(figure)


def test_the_title_names_each_arms_run_count() -> None:
    rungs = [build_rung("off", run, 1) for run in (1, 2, 3)]
    rungs += [build_rung("on", run, 1) for run in (1, 2)]
    figure = _plot_kv_ab(rungs)
    try:
        assert "median of off 3, on 2 runs" in figure.get_suptitle()
    finally:
        plt.close(figure)
