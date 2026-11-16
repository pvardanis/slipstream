"""Tests for the cold-start chart: one stacked bar per run, its phases end to end.

The cold-start-breakdown flow uploads the chart to S3 under the run's charts prefix,
so the plotter draws it straight to PNG bytes. Each run's bar stacks its phases in the
order the cold start runs through them, engine init split into its detail and the rest
of it, and the unattributed remainder last, so the bar ends at the run's total.
"""

import matplotlib.pyplot as plt
import pytest
from matplotlib.container import BarContainer

from slipstream_bench.report.cold_start_plotters import (
    _plot_cold_start,
    plot_cold_start_png,
)
from tests.report.cold_start_breakdowns import build_breakdown

# A PNG file leads with this 8-byte signature.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_the_chart_renders_to_png_bytes() -> None:
    png = plot_cold_start_png([build_breakdown(1), build_breakdown(2)], run_id="cs-1")

    assert png.startswith(_PNG_MAGIC)


def test_no_runs_are_refused_rather_than_drawn_blank() -> None:
    with pytest.raises(ValueError, match="no runs"):
        plot_cold_start_png([], run_id="cs-1")


def test_each_runs_bar_stacks_its_phases_with_engine_init_split_by_its_detail() -> None:
    # Given out of run order: the bars read top to bottom r1, r2.
    breakdowns = [
        build_breakdown(2, total_s=600.0, capacity_wait_s=60.0),
        build_breakdown(1),
    ]
    figure = _plot_cold_start(breakdowns, run_id="cs-1")
    try:
        [ax] = figure.axes
        segments = [c for c in ax.containers if isinstance(c, BarContainer)]
        assert [segment.get_label() for segment in segments] == [
            "Capacity wait",
            "Node launch",
            "Image pull",
            "Weight download",
            "torch.compile",
            "Graph capture",
            "Profiling run",
            "Engine init (rest)",
            "Readiness lag",
            "Unattributed",
        ]
        # Run 1's bar, the recorded cold start: engine init's 104.27 s splits into its
        # 44.91 + 4.0 + 0.1 s of detail and the 55.26 s rest.
        r1 = [segment.patches[0] for segment in segments]
        widths = [patch.get_width() for patch in r1]
        assert widths == pytest.approx(
            [3.0, 47.0, 264.0, 42.213, 44.91, 4.0, 0.1, 55.26, 7.0, 77.517]
        )
        # Each segment starts where the one before it ends, so the bar ends at the total.
        starts = [patch.get_x() for patch in r1]
        assert starts[0] == 0
        assert starts[1:] == pytest.approx(
            [start + width for start, width in zip(starts, widths)][:-1]
        )
        assert starts[-1] + widths[-1] == pytest.approx(545.0)
        r2_end = segments[-1].patches[1].get_x() + segments[-1].patches[1].get_width()
        assert r2_end == pytest.approx(600.0)
        assert [label.get_text() for label in ax.get_yticklabels()] == ["r1", "r2"]
        assert ax.yaxis_inverted()
        assert ax.get_xlabel() == "seconds (pod created → Ready)"
    finally:
        plt.close(figure)


def test_the_title_names_the_run_id_and_its_run_count() -> None:
    breakdowns = [build_breakdown(run) for run in (1, 2, 3)]
    figure = _plot_cold_start(breakdowns, run_id="cs-1")
    try:
        assert figure.axes[0].get_title() == "Cold-start breakdown — cs-1, 3 runs"
    finally:
        plt.close(figure)


def test_engine_init_detail_over_engine_init_by_its_rounding_draws_no_rest() -> None:
    # 44.91 + 4 + 0.1 s of detail in 49 s of engine init: graph capture is logged to the
    # whole second, so its rounding, not a misread marker, leaves no rest to draw.
    breakdowns = [build_breakdown(1, engine_init_s=49.0)]
    figure = _plot_cold_start(breakdowns, run_id="cs-1")
    try:
        [rest] = [
            c
            for c in figure.axes[0].containers
            if isinstance(c, BarContainer) and c.get_label() == "Engine init (rest)"
        ]
        assert rest.patches[0].get_width() == 0
    finally:
        plt.close(figure)


def test_engine_init_detail_longer_than_engine_init_is_refused() -> None:
    # 60 + 4 + 0.1 s of detail cannot sit inside 50 s of engine init: a misread marker,
    # which a backward segment would hide.
    breakdowns = [build_breakdown(1, engine_init_s=50.0, torch_compile_s=60.0)]

    with pytest.raises(ValueError, match="run 1's engine init detail .* exceeds"):
        plot_cold_start_png(breakdowns, run_id="cs-1")
