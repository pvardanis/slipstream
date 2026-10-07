"""Render a knob-sweep run's aggregated rows to Markdown and JSON tables.

The tables are the durable artifacts (ADR-0009): Markdown the human-readable view, JSON the
structured table later layers re-read. These renderers are pure and import no plotting
stack, so a caller rendering tables does not drag in matplotlib — the static PNGs and the
seaborn/matplotlib plotters that draw them live beside this in
:mod:`slipstream_bench.report.plotters`.
"""

import json

from slipstream_bench.report.aggregation import CeilingRow, RungRow

_TABLE_COLUMNS = (
    "max_num_seqs",
    "kv_cache_dtype",
    "prefix_caching",
    "prefix_share",
    "ceiling",
    "p95_ttft_ms",
    "p95_tpot_ms",
    "output_throughput",
    "timeout",
    "other",
    "oom",
    "num_preemptions",
)

# The diagnostic table's columns: the engine point, the rung's offered concurrency, its
# goodput fraction, the two p95 gates the fraction folds (ttft prefill-bound, tpot
# decode-bound), and its token rate — the cliff before aggregate_ceilings folds it.
_RUNG_TABLE_COLUMNS = (
    "max_num_seqs",
    "kv_cache_dtype",
    "prefix_caching",
    "prefix_share",
    "max_concurrency",
    "goodput_fraction",
    "p95_ttft_ms",
    "p95_tpot_ms",
    "output_throughput",
)


def rows_to_markdown(rows: list[CeilingRow]) -> str:
    """Render the aggregated ceiling rows as a GitHub-flavored Markdown table.

    The human-readable durable artifact: it renders inline in a PR or a run's notes,
    the failure cohorts flattened into columns and a not-captured None left blank.

    :param rows: the rows :func:`slipstream_bench.report.aggregation.aggregate_ceilings`
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

    :param rows: the rows :func:`slipstream_bench.report.aggregation.aggregate_ceilings`
        emitted, already sorted by point then prefix-share.
    :return: the rows as an indented JSON array.
    """
    return json.dumps(rows, indent=2)


def rungs_to_markdown(rungs: list[RungRow]) -> str:
    """Render the per-rung goodput rows as a GitHub-flavored Markdown table.

    The human-readable view of the diagnostic cliff: one line per ladder rung, the
    goodput fraction rounded for reading. The full-precision fraction stays in the
    JSON artifact.

    :param rungs: the rows :func:`slipstream_bench.report.aggregation.aggregate_rungs`
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

    :param rungs: the rows :func:`slipstream_bench.report.aggregation.aggregate_rungs`
        emitted, already sorted by point, then prefix-share, then offered concurrency.
    :return: the rows as an indented JSON array.
    """
    return json.dumps(rungs, indent=2)


def _cell(value: object) -> str:
    """Render one table cell, a not-captured None as an empty field."""
    return "" if value is None else str(value)


def _ms(value: float | None) -> str:
    """A p95 gate in whole milliseconds, blank when not captured — sub-ms noise is
    meaningless at the SLO scale the gates are set in."""
    return "" if value is None else f"{value:.0f}"


def _rate(value: float | None) -> str:
    """An output token rate to one decimal, blank when not captured."""
    return "" if value is None else f"{value:.1f}"


def _row_cells(row: CeilingRow) -> list[str]:
    """Flatten one ceiling row into its cells, :data:`_TABLE_COLUMNS` order."""
    failures = row["failures"]
    return [
        _cell(row["max_num_seqs"]),
        _cell(row["kv_cache_dtype"]),
        _cell(row["prefix_caching"]),
        _cell(row["prefix_share"]),
        _cell(row["ceiling"]),
        _ms(row["p95_ttft_ms"]),
        _ms(row["p95_tpot_ms"]),
        _rate(row["output_throughput"]),
        _cell(failures["timeout"]),
        _cell(failures["other"]),
        _cell(failures["oom"]),
        _cell(row["num_preemptions"]),
    ]


def _rung_cells(rung: RungRow) -> list[str]:
    """Flatten one rung into its cells, :data:`_RUNG_TABLE_COLUMNS` order.

    The goodput fraction rounds to three decimals for the human view — the messy
    goodput/throughput ratio reads cleanly here, its full precision kept in the JSON.
    The gates read in whole milliseconds and the token rate to one decimal.
    """
    return [
        _cell(rung["max_num_seqs"]),
        _cell(rung["kv_cache_dtype"]),
        _cell(rung["prefix_caching"]),
        _cell(rung["prefix_share"]),
        _cell(rung["max_concurrency"]),
        f"{rung['goodput_fraction']:.3f}",
        _ms(rung["p95_ttft_ms"]),
        _ms(rung["p95_tpot_ms"]),
        _rate(rung["output_throughput"]),
    ]
