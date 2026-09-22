"""Render a knob-sweep run's aggregated ceiling table to Markdown/JSON and a static PNG.

The ceiling table is the durable artifact and the PNG is disposable (ADR-0009), so the
Markdown and JSON are written from the same rows
:func:`slipstream_bench.sweep_aggregation.aggregate_ceilings` emits — Markdown the human-readable
view, JSON the structured table later layers re-read. The primary chart plots the
concurrency ceiling per engine point — x = max-num-seqs, series = kv-cache-dtype,
faceted by the combined caching/share condition — rendered offline through matplotlib's
Agg backend. Caching-off carries only its single share-0 baseline, so it holds one facet
while caching-on spans the swept shares: a ragged grid, no duplicated null cells.
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

_TABLE_COLUMNS = (
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
# the SLO it was read at, never bare. The ttft/tpot thresholds mirror the harness's
# cli_helpers.DEFAULT_GOODPUT and the 95% floor mirrors sweep_aggregation._GOODPUT_FLOOR
# (ADR-0009). The title also names the pinned burstiness the whole grid ran at — the
# aggregated rows do not carry it, so it is asserted here from the grid's pin, not read
# from the data. This label is a manual copy of those constants, kept in sync by hand.
_CEILING_AXIS_LABEL = "max sustained --max-concurrency at SLO"
_SLO_TITLE = (
    "concurrency ceiling — burstiness 1.0, "
    "goodput >= 95% (ttft <= 1000ms, tpot <= 50ms)"
)

# Caching-off reuses no prefix KV, so its prefix-share is a definitional n/a rather than
# a swept value — its own facet, ordered ahead of the swept shares.
_NO_SHARE_LABEL = "n/a"


def rows_to_markdown(rows: list[dict]) -> str:
    """Render the aggregated ceiling rows as a GitHub-flavored Markdown table.

    The human-readable durable artifact: it renders inline in a PR or a run's notes,
    the failure cohorts flattened into columns and a not-captured None left blank.

    :param rows: the rows :func:`slipstream_bench.sweep_aggregation.aggregate_ceilings`
        emitted, already sorted by point then prefix-share.
    :return: the table as one string: header, separator, one row per ceiling row.
    """
    header = "| " + " | ".join(_TABLE_COLUMNS) + " |"
    separator = "| " + " | ".join("---" for _ in _TABLE_COLUMNS) + " |"
    body = ["| " + " | ".join(_row_cells(row)) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def rows_to_json(rows: list[dict]) -> str:
    """Render the aggregated ceiling rows as indented JSON, the durable data artifact.

    Keeps the rows' nested structure — the failure cohorts and not-captured nulls —
    so the table re-reads as the same objects the aggregator emitted, unlike the
    flattened Markdown meant for a human reader.

    :param rows: the rows :func:`slipstream_bench.sweep_aggregation.aggregate_ceilings`
        emitted, already sorted by point then prefix-share.
    :return: the rows as an indented JSON array.
    """
    return json.dumps(rows, indent=2)


def write_artifacts(rows: list[dict], charts_dir: Path) -> dict[str, Path]:
    """Write the ceiling table and primary chart into a run's charts directory.

    The Markdown and JSON are the durable data artifacts; the PNG is the disposable
    view of them. The directory is created on the way out, so the run directory need
    not pre-hold it.

    :param rows: the rows :func:`slipstream_bench.sweep_aggregation.aggregate_ceilings` emitted.
    :param charts_dir: the ``bench/results/<run_id>/charts`` directory to write into.
    :return: the written paths, keyed ``markdown`` / ``json`` / ``png``.
    :raise ValueError: when ``rows`` is empty — an empty run holds no ceiling and must
        not be written as a header-only table and a blank plot.
    :raise OSError: when the directory cannot be made or an artifact cannot be written.
    """
    if not rows:
        raise ValueError(
            "cannot chart an empty ceiling table: the run aggregated no rows"
        )
    charts_dir = Path(charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": charts_dir / "ceiling-table.md",
        "json": charts_dir / "ceiling-table.json",
        "png": charts_dir / "ceiling-by-max-num-seqs.png",
    }
    paths["markdown"].write_text(rows_to_markdown(rows))
    paths["json"].write_text(rows_to_json(rows))
    grid = _plot_ceilings(rows)
    try:
        grid.savefig(paths["png"])
    finally:
        plt.close(grid.figure)
    return paths


def _cell(value: object) -> str:
    """Render one table cell, a not-captured None as an empty field."""
    return "" if value is None else str(value)


def _row_cells(row: dict) -> list[str]:
    """Flatten one ceiling row into its cells, :data:`_TABLE_COLUMNS` order."""
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


def _share_label(row: dict) -> str:
    """Label a row's share: the swept share, or n/a when caching is off."""
    return _NO_SHARE_LABEL if not row["prefix_caching"] else str(row["prefix_share"])


def _condition_label(row: dict) -> str:
    """Name a row's facet: the caching condition and, for caching-on, its share.

    Caching and share collapse into one facet axis rather than a caching x share
    cross-product, so the ragged grid holds only the conditions the sweep ran — one
    caching-off baseline facet and one per swept share — with no empty cross cells.
    """
    return f"caching {'on' if row['prefix_caching'] else 'off'} · share {_share_label(row)}"


def _ceiling_frame(rows: list[dict]) -> pd.DataFrame:
    """Shape the ceiling rows into the frame the primary chart facets over.

    Derives the facet label the plot reads — the combined caching/share condition —
    and keeps the ceiling as-is, so a point that held no rung plots as a gap rather
    than a zero. Carries prefix-caching and the share label alongside so the facets
    order the caching-off baseline ahead of the swept shares.
    """
    records = [
        {
            "max_num_seqs": row["max_num_seqs"],
            "kv_cache_dtype": row["kv_cache_dtype"],
            "prefix_caching": "on" if row["prefix_caching"] else "off",
            "prefix_share": _share_label(row),
            "condition": _condition_label(row),
            "ceiling": row["ceiling"],
        }
        for row in rows
    ]
    return pd.DataFrame.from_records(records)


def _condition_order(frame: pd.DataFrame) -> list[str]:
    """Order the facets: the caching-off baseline first, then swept shares by number."""
    conditions = frame[
        ["condition", "prefix_caching", "prefix_share"]
    ].drop_duplicates()

    def sort_key(row: tuple) -> tuple[int, int]:
        caching_rank = 0 if row.prefix_caching == "off" else 1
        share_rank = (
            -1 if row.prefix_share == _NO_SHARE_LABEL else int(row.prefix_share)
        )
        return (caching_rank, share_rank)

    ordered = sorted(conditions.itertuples(index=False), key=sort_key)
    return [row.condition for row in ordered]


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
        col="condition",
        col_order=_condition_order(frame),
        col_wrap=4,
        marker="o",
    )
    grid.set_axis_labels("max-num-seqs", _CEILING_AXIS_LABEL)
    grid.set_titles("{col_name}")
    grid.figure.suptitle(_SLO_TITLE)
    grid.tight_layout()
    return grid
