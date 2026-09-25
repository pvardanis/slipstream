"""Render a knob-sweep run's aggregated tables to Markdown/JSON and two static PNGs.

The tables are the durable artifacts and the PNGs disposable (ADR-0009), so each plot's
Markdown and JSON are written from the same rows the aggregator emits — Markdown the
human-readable view, JSON the structured table later layers re-read. Two charts, a pair:
the primary plots the concurrency ceiling per engine point
(:func:`slipstream_bench.sweep.aggregation.aggregate_ceilings`) — x = max-num-seqs,
series = kv-cache-dtype, faceted by the combined caching/share condition; the diagnostic
plots the goodput cliff each ceiling was read off
(:func:`slipstream_bench.sweep.aggregation.aggregate_rungs`) — x = --max-concurrency,
y = goodput fraction, series = prefix-share, one facet per engine point, with the 95%
floor drawn as a reference line. Both render offline through matplotlib's Agg backend.
Caching-off carries only its single share-0 baseline, so it holds one primary facet while
caching-on spans the swept shares: a ragged grid, no duplicated null cells.
"""

import json
from pathlib import Path
from typing import TypedDict

import matplotlib

# Select the non-interactive Agg backend before importing pyplot/seaborn: the chart is
# an offline batch artifact with no display server, and seaborn binds the backend at
# import. The imports below therefore follow this call rather than leading the module.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from slipstream_bench.sweep.aggregation import (
    _GOODPUT_FLOOR,
    CeilingRow,
    RungRow,
)

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
# default goodput (bench/load-sweep.yaml) and the 95% floor mirrors
# sweep.aggregation._GOODPUT_FLOOR (ADR-0009). The title also names the pinned
# burstiness the whole grid ran at — the aggregated rows do not carry it, so it is
# asserted here from the grid's pin, not read from the data. This label is a manual
# copy of those constants, kept in sync by hand.
_CEILING_AXIS_LABEL = "max sustained --max-concurrency at SLO"
_SLO_TITLE = (
    "concurrency ceiling — burstiness 1.0, "
    "goodput >= 95% (ttft <= 1000ms, tpot <= 50ms)"
)

# The diagnostic cliff reads the same SLO. The reference line is drawn at
# sweep.aggregation._GOODPUT_FLOOR itself (imported, not copied), so the line the cliff
# crosses always marks the aggregator's true floor. The title's "0.95"/"floor" text and
# the ttft/tpot thresholds stay hand-mirrored prose — the rung rows carry the goodput
# fraction, not the SLO it was read at.
_CLIFF_AXIS_LABEL = "goodput fraction (met SLO / completed)"
_CLIFF_TITLE = (
    "goodput cliff — burstiness 1.0, floor 0.95 (ttft <= 1000ms, tpot <= 50ms)"
)

# The diagnostic table's columns: the engine point, the rung's offered concurrency, and
# its goodput fraction — the cliff before aggregate_ceilings folds it to one number.
_RUNG_TABLE_COLUMNS = (
    "max_num_seqs",
    "kv_cache_dtype",
    "prefix_caching",
    "prefix_share",
    "max_concurrency",
    "goodput_fraction",
)

# Caching-off reuses no prefix KV, so its prefix-share is a definitional n/a rather than
# a swept value — its own facet, ordered ahead of the swept shares.
_NO_SHARE_LABEL = "n/a"


def rows_to_markdown(rows: list[CeilingRow]) -> str:
    """Render the aggregated ceiling rows as a GitHub-flavored Markdown table.

    The human-readable durable artifact: it renders inline in a PR or a run's notes,
    the failure cohorts flattened into columns and a not-captured None left blank.

    :param rows: the rows :func:`slipstream_bench.sweep.aggregation.aggregate_ceilings`
        emitted, already sorted by point then prefix-share.
    :return: the table as one string: header, separator, one row per ceiling row.
    """
    header = "| " + " | ".join(_TABLE_COLUMNS) + " |"
    separator = "| " + " | ".join("---" for _ in _TABLE_COLUMNS) + " |"
    body = ["| " + " | ".join(_row_cells(row)) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def rows_to_json(rows: list[CeilingRow]) -> str:
    """Render the aggregated ceiling rows as indented JSON, the durable data artifact.

    Keeps the rows' nested structure — the failure cohorts and not-captured nulls —
    so the table re-reads as the same objects the aggregator emitted, unlike the
    flattened Markdown meant for a human reader.

    :param rows: the rows :func:`slipstream_bench.sweep.aggregation.aggregate_ceilings`
        emitted, already sorted by point then prefix-share.
    :return: the rows as an indented JSON array.
    """
    return json.dumps(rows, indent=2)


def rungs_to_markdown(rungs: list[RungRow]) -> str:
    """Render the per-rung goodput rows as a GitHub-flavored Markdown table.

    The human-readable view of the diagnostic cliff: one line per ladder rung, the
    goodput fraction rounded for reading. The full-precision fraction stays in the
    JSON artifact.

    :param rungs: the rows :func:`slipstream_bench.sweep.aggregation.aggregate_rungs`
        emitted, already sorted by point, then prefix-share, then offered concurrency.
    :return: the table as one string: header, separator, one row per rung.
    """
    header = "| " + " | ".join(_RUNG_TABLE_COLUMNS) + " |"
    separator = "| " + " | ".join("---" for _ in _RUNG_TABLE_COLUMNS) + " |"
    body = ["| " + " | ".join(_rung_cells(rung)) + " |" for rung in rungs]
    return "\n".join([header, separator, *body])


def rungs_to_json(rungs: list[RungRow]) -> str:
    """Render the per-rung goodput rows as indented JSON, the durable cliff data.

    Keeps the full-precision goodput fraction the Markdown rounds, so the diagnostic
    table re-reads as the same objects the aggregator emitted.

    :param rungs: the rows :func:`slipstream_bench.sweep.aggregation.aggregate_rungs`
        emitted, already sorted by point, then prefix-share, then offered concurrency.
    :return: the rows as an indented JSON array.
    """
    return json.dumps(rungs, indent=2)


def write_artifacts(
    rows: list[CeilingRow], rungs: list[RungRow], charts_dir: Path
) -> dict[str, Path]:
    """Write both tables and both charts into a run's charts directory.

    The Markdown and JSON are the durable data artifacts; the PNGs are the disposable
    view of them. The primary chart plots the ceiling per point, the diagnostic the
    goodput cliff each ceiling was read off. The directory is created on the way out,
    so the run directory need not pre-hold it.

    :param rows: the rows :func:`slipstream_bench.sweep.aggregation.aggregate_ceilings`
        emitted.
    :param rungs: the rows :func:`slipstream_bench.sweep.aggregation.aggregate_rungs`
        emitted.
    :param charts_dir: the ``bench/results/<run_id>/charts`` directory to write into.
    :return: the written paths, keyed ``markdown`` / ``json`` / ``png`` for the ceiling
        table and its plot, ``rungs_markdown`` / ``rungs_json`` / ``rungs_png`` for the
        cliff table and its plot.
    :raise ValueError: when ``rows`` or ``rungs`` is empty — an empty run holds neither a
        ceiling nor a cliff and must not be written as a header-only table and a blank
        plot. The two are non-empty together for a run aggregated off one directory, but
        the parameters are independent, so each is guarded.
    :raise OSError: when the directory cannot be made or an artifact cannot be written.
    """
    if not rows:
        raise ValueError(
            "cannot chart an empty ceiling table: the run aggregated no rows"
        )
    if not rungs:
        raise ValueError(
            "cannot chart an empty cliff table: the run aggregated no rungs"
        )
    charts_dir = Path(charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": charts_dir / "ceiling-table.md",
        "json": charts_dir / "ceiling-table.json",
        "png": charts_dir / "ceiling-by-max-num-seqs.png",
        "rungs_markdown": charts_dir / "goodput-cliff.md",
        "rungs_json": charts_dir / "goodput-cliff.json",
        "rungs_png": charts_dir / "goodput-by-max-concurrency.png",
    }
    paths["markdown"].write_text(rows_to_markdown(rows))
    paths["json"].write_text(rows_to_json(rows))
    paths["rungs_markdown"].write_text(rungs_to_markdown(rungs))
    paths["rungs_json"].write_text(rungs_to_json(rungs))
    _save_figure(_plot_ceilings(rows), paths["png"])
    _save_figure(_plot_cliffs(rungs), paths["rungs_png"])
    return paths


def _save_figure(grid: sns.FacetGrid, path: Path) -> None:
    """Save a grid to a PNG and close its figure, freeing it even on a write error."""
    try:
        grid.savefig(path)
    finally:
        plt.close(grid.figure)


def _cell(value: object) -> str:
    """Render one table cell, a not-captured None as an empty field."""
    return "" if value is None else str(value)


def _row_cells(row: CeilingRow) -> list[str]:
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


class _ShareRow(TypedDict):
    """The caching/share pair a share label reads, shared by ceiling and rung rows."""

    prefix_caching: bool
    prefix_share: int


def _share_label(row: _ShareRow) -> str:
    """Label a row's share: the swept share, or n/a when caching is off."""
    return _NO_SHARE_LABEL if not row["prefix_caching"] else str(row["prefix_share"])


def _condition_label(row: CeilingRow) -> str:
    """Name a row's facet: the caching condition and, for caching-on, its share.

    Caching and share collapse into one facet axis rather than a caching x share
    cross-product, so the ragged grid holds only the conditions the sweep ran — one
    caching-off baseline facet and one per swept share — with no empty cross cells.
    """
    return f"caching {'on' if row['prefix_caching'] else 'off'} · share {_share_label(row)}"


def _ceiling_frame(rows: list[CeilingRow]) -> pd.DataFrame:
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

    def sort_key(row: dict) -> tuple[int, int]:
        caching_rank = 0 if row["prefix_caching"] == "off" else 1
        share_rank = (
            -1 if row["prefix_share"] == _NO_SHARE_LABEL else int(row["prefix_share"])
        )
        return (caching_rank, share_rank)

    ordered = sorted(conditions.to_dict("records"), key=sort_key)
    return [str(row["condition"]) for row in ordered]


def _plot_ceilings(rows: list[CeilingRow]) -> sns.FacetGrid:
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


def _rung_cells(rung: RungRow) -> list[str]:
    """Flatten one rung into its cells, :data:`_RUNG_TABLE_COLUMNS` order.

    The goodput fraction rounds to three decimals for the human view — the messy
    goodput/throughput ratio reads cleanly here, its full precision kept in the JSON.
    """
    return [
        _cell(rung["max_num_seqs"]),
        _cell(rung["kv_cache_dtype"]),
        _cell(rung["prefix_caching"]),
        _cell(rung["prefix_share"]),
        _cell(rung["max_concurrency"]),
        f"{rung['goodput_fraction']:.3f}",
    ]


def _point_label(rung: RungRow) -> str:
    """Name a rung's facet: the engine point one Tier-1 redeploy measured."""
    caching = "on" if rung["prefix_caching"] else "off"
    return f"mns{rung['max_num_seqs']} · {rung['kv_cache_dtype']} · caching {caching}"


def _cliff_frame(rungs: list[RungRow]) -> pd.DataFrame:
    """Shape the rung rows into the frame the diagnostic chart facets over.

    Derives the facet label (the engine point) and the hue label (the swept share, or
    n/a when caching is off), keeping each rung's offered concurrency and goodput so the
    plot draws the cliff per point.
    """
    records = [
        {
            "point": _point_label(rung),
            "prefix_share": _share_label(rung),
            "max_concurrency": rung["max_concurrency"],
            "goodput_fraction": rung["goodput_fraction"],
        }
        for rung in rungs
    ]
    return pd.DataFrame.from_records(records)


def _point_order(frame: pd.DataFrame) -> list[str]:
    """Order the facets by first appearance — the point key aggregate_rungs sorted on."""
    return list(dict.fromkeys(frame["point"]))


def _plot_cliffs(rungs: list[RungRow]) -> sns.FacetGrid:
    """Draw the diagnostic goodput-cliff chart onto a faceted grid.

    One facet per engine point, a line per swept share, the 95% floor drawn as the
    reference line the cliff crosses. The offered-concurrency axis is log-2 scaled so
    the doubling ladder spaces evenly.

    :param rungs: the aggregated per-rung rows.
    :return: the seaborn FacetGrid, ready to save — the caller closes its figure.
    """
    frame = _cliff_frame(rungs)
    grid = sns.relplot(
        data=frame,
        kind="line",
        x="max_concurrency",
        y="goodput_fraction",
        hue="prefix_share",
        col="point",
        col_order=_point_order(frame),
        col_wrap=4,
        marker="o",
    )
    grid.refline(y=_GOODPUT_FLOOR, color="crimson", linestyle="--")
    for ax in grid.axes.flat:
        ax.set_xscale("log", base=2)
    grid.set_axis_labels("--max-concurrency", _CLIFF_AXIS_LABEL)
    grid.set_titles("{col_name}")
    grid.figure.suptitle(_CLIFF_TITLE)
    grid.tight_layout()
    return grid
