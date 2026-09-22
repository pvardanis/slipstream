"""Render a knob-sweep run's aggregated ceiling table to CSV/JSON and a static PNG.

The ceiling table is the durable artifact and the PNG is disposable (ADR-0009), so the
CSV and JSON are written from the same rows
:func:`slipstream_bench.sweep_aggregation.aggregate` emits. The primary chart plots the
concurrency ceiling per engine point — x = max-num-seqs, series = kv-cache-dtype,
faceted by prefix-caching x prefix-share — rendered offline through matplotlib's Agg
backend. Caching-off carries only its single share-0 baseline, so its row holds one
facet while caching-on spans the swept shares: a ragged grid, no duplicated null cells.
"""

import json
from pathlib import Path

import matplotlib

# Select the non-interactive Agg backend before importing pyplot/seaborn: the chart is
# an offline batch artifact with no display server, and seaborn binds the backend at
# import. The imports below therefore follow this call rather than leading the module.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

_CSV_COLUMNS = (
    "max_num_seqs",
    "kv_cache_dtype",
    "prefix_caching",
    "prefix_share",
    "ceiling",
    "timeout",
    "other",
    "oom",
    "num_preemptions",
)

# The ceiling axis names its SLO predicate: the KB pairs a goodput/ceiling number with
# the SLO it was read at, never bare. These thresholds mirror the shared SLO the
# aggregator reads the ceiling off (ADR-0009: goodput >= 95% meeting ttft<=1000ms and
# tpot<=50ms) — the same source cli_helpers.DEFAULT_GOODPUT feeds the harness.
_CEILING_AXIS_LABEL = "max sustained --max-concurrency at SLO"
_SLO_TITLE = "concurrency ceiling — goodput >= 95% (ttft <= 1000ms, tpot <= 50ms)"

# Caching-off reuses no prefix KV, so its prefix-share is a definitional n/a rather than
# a swept value — its own facet column, ordered ahead of the swept shares.
_NO_SHARE_LABEL = "n/a"


def _cell(value: object) -> str:
    """Render one CSV cell, a not-captured None as an empty field."""
    return "" if value is None else str(value)


def _row_cells(row: dict) -> list[str]:
    """Flatten one ceiling row into its CSV cells, :data:`_CSV_COLUMNS` order."""
    failures = row["failures"]
    return [
        _cell(row["max_num_seqs"]),
        _cell(row["kv_cache_dtype"]),
        _cell(row["prefix_caching"]),
        _cell(row["prefix_share"]),
        _cell(row["ceiling"]),
        _cell(failures["timeout"]),
        _cell(failures["other"]),
        _cell(failures["oom"]),
        _cell(row["num_preemptions"]),
    ]


def rows_to_csv(rows: list[dict]) -> str:
    """Render the aggregated ceiling rows as CSV, the durable data artifact.

    :param rows: the rows :func:`slipstream_bench.sweep_aggregation.aggregate`
        emitted, already sorted by point then prefix-share.
    :return: the CSV text: one header line, then one line per row, the failure
        cohorts flattened and a not-captured None left as an empty field.
    """
    header = ",".join(_CSV_COLUMNS)
    body = [",".join(_row_cells(row)) for row in rows]
    return "\n".join([header, *body])


def rows_to_json(rows: list[dict]) -> str:
    """Render the aggregated ceiling rows as indented JSON, the durable data artifact.

    Keeps the rows' nested structure — the failure cohorts and not-captured nulls —
    so the table re-reads as the same objects the aggregator emitted, unlike the
    flattened CSV meant for a spreadsheet.

    :param rows: the rows :func:`slipstream_bench.sweep_aggregation.aggregate`
        emitted, already sorted by point then prefix-share.
    :return: the rows as an indented JSON array.
    """
    return json.dumps(rows, indent=2)


def _share_label(row: dict) -> str:
    """Label a row's facet column: the swept share, or n/a when caching is off."""
    return _NO_SHARE_LABEL if not row["prefix_caching"] else str(row["prefix_share"])


def _ceiling_frame(rows: list[dict]) -> pd.DataFrame:
    """Shape the ceiling rows into the frame the primary chart facets over.

    Derives the two facet labels the plot reads — prefix-caching as on/off and the
    share column (n/a for the caching-off baseline) — and keeps the ceiling as-is, so
    a point that held no rung plots as a gap rather than a zero.
    """
    records = [
        {
            "max_num_seqs": row["max_num_seqs"],
            "kv_cache_dtype": row["kv_cache_dtype"],
            "prefix_caching": "on" if row["prefix_caching"] else "off",
            "prefix_share": _share_label(row),
            "ceiling": row["ceiling"],
        }
        for row in rows
    ]
    return pd.DataFrame.from_records(records)


def _share_order(frame: pd.DataFrame) -> list[str]:
    """Order the facet columns: the caching-off n/a baseline, then swept shares."""
    swept = sorted(
        {label for label in frame["prefix_share"] if label != _NO_SHARE_LABEL},
        key=int,
    )
    has_baseline = (frame["prefix_share"] == _NO_SHARE_LABEL).any()
    return ([_NO_SHARE_LABEL] if has_baseline else []) + swept


def _plot_ceilings(rows: list[dict]) -> sns.FacetGrid:
    """Draw the primary ceiling chart onto a faceted grid.

    :param rows: the aggregated ceiling rows.
    :return: the seaborn FacetGrid, ready to save — the caller closes its figure.
    """
    frame = _ceiling_frame(rows)
    grid = sns.relplot(
        data=frame,
        kind="line",
        x="max_num_seqs",
        y="ceiling",
        hue="kv_cache_dtype",
        row="prefix_caching",
        col="prefix_share",
        col_order=_share_order(frame),
        marker="o",
    )
    grid.set_axis_labels("max-num-seqs", _CEILING_AXIS_LABEL)
    grid.set_titles("caching {row_name} · prefix-share {col_name}")
    grid.figure.suptitle(_SLO_TITLE)
    grid.tight_layout()
    return grid


def write_artifacts(rows: list[dict], charts_dir: Path) -> dict[str, Path]:
    """Write the ceiling table and primary chart into a run's charts directory.

    The CSV and JSON are the durable data artifacts; the PNG is the disposable view of
    them. The directory is created on the way out, so the run directory need not
    pre-hold it.

    :param rows: the rows :func:`slipstream_bench.sweep_aggregation.aggregate` emitted.
    :param charts_dir: the ``bench/results/<run_id>/charts`` directory to write into.
    :return: the written paths, keyed ``csv`` / ``json`` / ``png``.
    """
    charts_dir = Path(charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": charts_dir / "ceiling-table.csv",
        "json": charts_dir / "ceiling-table.json",
        "png": charts_dir / "ceiling-by-max-num-seqs.png",
    }
    paths["csv"].write_text(rows_to_csv(rows))
    paths["json"].write_text(rows_to_json(rows))
    grid = _plot_ceilings(rows)
    grid.savefig(paths["png"])
    plt.close(grid.figure)
    return paths
