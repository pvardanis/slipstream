"""Plot a cold-start-breakdown run's cold starts as one stacked bar per run, to one PNG.

Each run is a horizontal bar, read top to bottom in run order, that stacks the run's
phases left to right in the order the cold start runs through them (ADR-0030). Engine
init's segment is split by its detail — torch.compile, graph capture, the profiling
run — and the rest of engine init, in shades of one colour so they read as one phase.
The unattributed remainder comes last, so each bar ends at its run's pod-created →
Ready total, and the bars side by side show the runs' spread. The chart renders offline
through matplotlib's Agg backend.
"""

import matplotlib

# Select the non-interactive Agg backend before importing pyplot: the chart is an offline
# batch artifact with no display server.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from slipstream_bench.contract.cold_start_record import (
    ENGINE_INIT_DETAIL,
    PHASES,
    ColdStartBreakdown,
)
from slipstream_bench.report.cold_start_aggregation import FIELD_LABELS
from slipstream_bench.report.plotters import get_figure_png_bytes

# The phases stacked before engine init's split segment, and after it, where the
# unattributed remainder closes the bar.
_ENGINE_INIT = "engine_init_s"
_BEFORE_ENGINE_INIT = PHASES[: PHASES.index(_ENGINE_INIT)]
_AFTER_ENGINE_INIT = (*PHASES[PHASES.index(_ENGINE_INIT) + 1 :], "unattributed_s")

# The segment that holds the part of engine init its detail does not cover, and its label.
_ENGINE_INIT_REST = "engine_init_rest_s"
_SEGMENT_LABELS = {**FIELD_LABELS, _ENGINE_INIT_REST: "Engine init (rest)"}
# How far engine init's detail may overrun engine init before it counts as a misread
# marker: graph capture is logged to the whole second and summed over two captures.
_ROUNDING_S = 1.0

# Each segment's colour: the phases from tab10, engine init's split in shades of orange,
# and the unattributed remainder grey, so it reads as no phase.
_COLOURS = {
    "capacity_wait_s": "#1f77b4",
    "node_launch_s": "#2ca02c",
    "image_pull_s": "#9467bd",
    "weight_download_s": "#8c564b",
    "torch_compile_s": "#d95f02",
    "graph_capture_s": "#fd8d3c",
    "profiling_run_s": "#fdbe85",
    _ENGINE_INIT_REST: "#ff7f0e",
    "readiness_lag_s": "#17becf",
    "unattributed_s": "#7f7f7f",
}


def plot_cold_start_png(breakdowns: list[ColdStartBreakdown], *, run_id: str) -> bytes:
    """Render the runs' stacked-bar chart straight to PNG bytes.

    :param breakdowns: every run's breakdown, in any order.
    :param run_id: the run id the runs were measured under, for the title.
    :return: the chart encoded as PNG bytes.
    :raise ValueError: when ``breakdowns`` is empty, so there is nothing to draw, or a
        run's engine init detail overruns its engine init by more than its rounding.
    """
    if not breakdowns:
        raise ValueError("cannot chart a cold start with no runs")
    return get_figure_png_bytes(_plot_cold_start(breakdowns, run_id=run_id))


def _plot_cold_start(breakdowns: list[ColdStartBreakdown], *, run_id: str) -> Figure:
    """Draw one stacked bar per run onto one figure — the caller closes it."""
    ordered = sorted(breakdowns, key=lambda breakdown: breakdown.run)
    runs = [f"r{breakdown.run}" for breakdown in ordered]
    rows = [_get_segments(breakdown) for breakdown in ordered]
    figure, ax = plt.subplots(figsize=(11, 1.2 + 0.6 * len(ordered)))
    starts = [0.0] * len(ordered)
    for segment in rows[0]:
        widths = [row[segment] for row in rows]
        ax.barh(
            runs,
            widths,
            left=starts,
            label=_SEGMENT_LABELS[segment],
            color=_COLOURS[segment],
        )
        starts = [start + width for start, width in zip(starts, widths, strict=True)]
    ax.invert_yaxis()
    ax.set_xlabel("seconds (pod created → Ready)")
    runs_label = "run" if len(ordered) == 1 else "runs"
    ax.set_title(f"Cold-start breakdown — {run_id}, {len(ordered)} {runs_label}")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize="small")
    figure.tight_layout()
    return figure


def _get_segments(breakdown: ColdStartBreakdown) -> dict[str, float]:
    """Split one run's record into its bar's segments, in stacking order, by field.

    A rest under zero by no more than the markers' rounding is drawn as none.

    :raise ValueError: when the run's engine init detail overruns its engine init by
        more than the markers' rounding, which means a marker was misread; drawn, the
        rest would stack backwards.
    """
    record = breakdown.to_record()
    detail = {field: record[field] for field in ENGINE_INIT_DETAIL}
    rest = record[_ENGINE_INIT] - sum(detail.values())
    if rest < -_ROUNDING_S:
        raise ValueError(
            f"cold-start run {breakdown.run}'s engine init detail "
            f"({sum(detail.values()):g} s) exceeds its engine init "
            f"({record[_ENGINE_INIT]:g} s): a marker was misread"
        )
    return {
        **{field: record[field] for field in _BEFORE_ENGINE_INIT},
        **detail,
        _ENGINE_INIT_REST: max(rest, 0.0),
        **{field: record[field] for field in _AFTER_ENGINE_INIT},
    }
