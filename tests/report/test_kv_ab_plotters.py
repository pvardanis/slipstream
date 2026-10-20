"""Tests for the kv-ab chart: three panels comparing the arms across the concurrency ladder.

The A/B's render uploads the chart to S3 and embeds it on the flow run page, so the plotter
draws it straight to PNG bytes. Each panel plots one metric against ``--max-concurrency``,
a line per arm through its runs' medians, and the goodput panel draws the floor the
ceiling is read at.
"""

import matplotlib.pyplot as plt
import numpy as np
import pytest

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
