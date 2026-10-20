"""Plot a kv-cache-extension-ab run's arms against the concurrency ladder as one PNG.

Three panels share the ``--max-concurrency`` axis (log-2, so the doubling ladder spaces
evenly) and colour by arm: the combined GPU plus CPU-tier hit rate, which shows whether the
offload served anything; p95 TTFT, where a served prefix skips its prefill; and the goodput
fraction, with the floor each arm run's ceiling is read at drawn dashed. Each arm's line runs
through its runs' medians inside a band spanning their range, and each run is a dot, nudged
sideways so runs that read alike stay apart. The chart renders offline through matplotlib's
Agg backend.
"""

import io

import matplotlib

# Select the non-interactive Agg backend before importing pyplot/seaborn: the chart is an
# offline batch artifact with no display server, and seaborn binds the backend at import.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.figure import Figure

from slipstream_bench.report.aggregation import _GOODPUT_FLOOR
from slipstream_bench.report.kv_ab_aggregation import KvAbRung

# Each panel's metric and its y-axis label, left to right.
_PANELS = (
    ("combined_hit_rate", "combined hit rate (GPU + CPU tier)"),
    ("p95_ttft_ms", "p95 TTFT (ms)"),
    ("goodput_fraction", "goodput fraction (met SLO / completed)"),
)

# The arms' colours, offload off grey and on blue, so the two read apart in every panel.
_PALETTE = {"off": "#7f7f7f", "on": "#1f77b4"}

# How far each run's dot is nudged sideways off its rung, as a share of the gap between
# adjacent rungs on the log-2 axis.
_JITTER = 0.08


def plot_kv_ab_png(rungs: list[KvAbRung]) -> bytes:
    """Render the A/B's three-panel chart straight to PNG bytes.

    :param rungs: every rung of the A/B's arm runs.
    :return: the chart encoded as PNG bytes.
    :raise ValueError: when ``rungs`` is empty, so there is nothing to draw.
    """
    if not rungs:
        raise ValueError("cannot chart a kv-ab run with no rungs")
    figure = _plot_kv_ab(rungs)
    buffer = io.BytesIO()
    try:
        figure.savefig(buffer, format="png")
    finally:
        plt.close(figure)
    return buffer.getvalue()


def _plot_kv_ab(rungs: list[KvAbRung]) -> Figure:
    """Draw the three panels onto one figure — the caller closes it."""
    frame = _rung_frame(rungs)
    ladder = sorted(frame["max_concurrency"].unique())
    figure, axes = plt.subplots(1, len(_PANELS), figsize=(15, 4.6))
    for ax, (metric, label) in zip(axes, _PANELS, strict=True):
        # Log-scale the axis before drawing, so seaborn nudges each run's dot in log space:
        # an even share of the gap between rungs at every rung of the doubling ladder.
        ax.set_xscale("log", base=2)
        sns.lineplot(
            data=frame,
            x="max_concurrency",
            y=metric,
            hue="arm",
            estimator="median",
            errorbar=("pi", 100),
            marker="o",
            palette=_PALETTE,
            ax=ax,
        )
        sns.stripplot(
            data=frame,
            x="max_concurrency",
            y=metric,
            hue="arm",
            native_scale=True,
            jitter=_JITTER,
            palette=_PALETTE,
            alpha=0.5,
            size=5,
            legend=False,
            ax=ax,
        )
        ax.set_xticks(ladder, labels=[str(cap) for cap in ladder])
        ax.set_xlabel("--max-concurrency")
        ax.set_ylabel(label)
        if metric == "goodput_fraction":
            ax.axhline(
                _GOODPUT_FLOOR, linestyle="--", color="crimson", label="goodput floor"
            )
        ax.legend(title="offload")
    figure.suptitle(_get_title(frame))
    figure.tight_layout()
    return figure


def _rung_frame(rungs: list[KvAbRung]) -> pd.DataFrame:
    """Shape the rungs into one row per arm run's rung, the metrics the panels plot."""
    return pd.DataFrame.from_records(
        [
            {
                "arm": rung.arm,
                "run": rung.run,
                "max_concurrency": rung.cell.max_concurrency,
                "prefix_share": rung.cell.prefix_share,
                "combined_hit_rate": rung.combined_hit_rate,
                "p95_ttft_ms": rung.cell.p95_ttft_ms,
                "goodput_fraction": rung.cell.goodput_fraction,
            }
            for rung in rungs
        ]
    )


def _get_title(frame: pd.DataFrame) -> str:
    """Name what the chart draws: the runs per arm and the prefix share they ran."""
    runs = frame.groupby("arm")["run"].nunique().max()
    shares = ", ".join(str(share) for share in sorted(frame["prefix_share"].unique()))
    return (
        f"KV cache offload A/B — prefix share {shares}: median of {runs} runs per arm, "
        f"band = range, dots = runs"
    )
