"""Render a cold-start-breakdown run's spreads as a Markdown table.

The table sits on the flow run page: one row per phase, in the order the cold start runs
through them (ADR-0030), with its median and, bracketed, its range across the runs.
Engine init's detail sits beneath its row, marked as inside it rather than added to it,
then the unattributed remainder and the total. Like :mod:`slipstream_bench.report.chart`
the renderer is pure and imports no plotting stack.
"""

from slipstream_bench.contract.cold_start_record import ENGINE_INIT_DETAIL
from slipstream_bench.report.chart import to_markdown_table
from slipstream_bench.report.cold_start_aggregation import (
    FIELD_LABELS,
    ColdStartSummary,
)
from slipstream_bench.report.spread import format_spread

_TOTAL = "total_s"
_COLUMNS = ("phase", "seconds")
# Seconds print to one place: the markers carry up to a second of rounding (ADR-0030).
_SECONDS = "{:.1f}"


def cold_start_spreads_to_markdown(summary: ColdStartSummary) -> str:
    """Render each field's spread as a Markdown table row, ``median [min–max]``.

    :param summary: the summary
        :func:`slipstream_bench.report.cold_start_aggregation.summarize_cold_starts`
        emitted.
    :return: the table: header, separator, one row per phase and detail, then the total.
    """
    spreads = summary["spreads"]
    body = [
        [_label(field), format_spread(spreads[field], _SECONDS)]
        for field in FIELD_LABELS
        if field != _TOTAL
    ]
    total = [
        f"**{FIELD_LABELS[_TOTAL]}**",
        f"**{format_spread(spreads[_TOTAL], _SECONDS)}**",
    ]
    return to_markdown_table(_COLUMNS, [*body, total])


def _label(field: str) -> str:
    """Name a field's row, marking engine init's detail as inside the row above it."""
    label = FIELD_LABELS[field]
    return f"↳ {label}" if field in ENGINE_INIT_DETAIL else label
