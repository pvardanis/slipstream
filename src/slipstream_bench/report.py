"""Join the L0 harness's three arms into the baseline $/1M-at-SLO report.

The report is the L0 deliverable: self-hosted $/1M vs commercial $/1M for one
config, segmented per concurrency level (the run's request_rate is the L0
concurrency proxy) and prefix-share bucket, with cold and warm cache both
labelled and TTFT/TPOT p95 and p99 both reported — never one blended number.

It consumes the JSON the three tools already emit rather than re-pricing:
prefix-cache records are the spine (they alone carry the cold/warm label and the
SLO tail), self-hosted cost joins onto each by the shared result-file ``source``,
and commercial cost joins by the segment it shares — request_rate and
prefix_share — since the commercial arm is a separate sweep against a per-token
API with no result-file in common. The output is a pure function of its inputs.
"""

import json
from pathlib import Path

_SLO_KEYS = (
    "request_goodput",
    "p95_ttft_ms",
    "p99_ttft_ms",
    "p95_tpot_ms",
    "p99_tpot_ms",
)


class ReportError(Exception):
    """A report input that cannot be joined into a baseline row."""


def load_records(path: Path) -> list[dict]:
    """Load one arm's tool output into its list of records.

    The ``cost`` and ``commercial-cost`` tools emit a JSON array; ``prefix-cache``
    emits one object per run. Both load here: an array yields its elements, a lone
    object yields a one-element list, and a non-object element fails fast rather
    than reaching the join as a nothing.

    :param path: the JSON file a tool wrote.
    :return: the records it holds.
    :raise ReportError: when the file is unreadable, not JSON, or holds a
        non-object where a record is expected.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReportError(f"cannot read {path}: {error}") from error
    records = data if isinstance(data, list) else [data]
    for record in records:
        if not isinstance(record, dict):
            raise ReportError(f"{path}: record is not a JSON object")
    return records


def build_report(
    *,
    self_hosted_cost: list[dict],
    commercial_cost: list[dict],
    prefix_cache: list[dict],
) -> list[dict]:
    """Join the three arms into one baseline row per prefix-cache run.

    :param self_hosted_cost: the ``cost`` tool's records, joined by ``source``.
    :param commercial_cost: the ``commercial-cost`` tool's records, joined by
        the (request_rate, prefix_share) segment.
    :param prefix_cache: the ``prefix-cache`` tool's records — the spine that
        carries the cold/warm label, the segment keys, and the SLO tail.
    :return: one row per prefix-cache record.
    """
    if not prefix_cache:
        raise ReportError("no prefix-cache records: nothing to report")
    rows = [
        _build_row(record, self_hosted_cost, commercial_cost) for record in prefix_cache
    ]
    return sorted(rows, key=_sort_key)


# Cold precedes warm within a segment — the empty-cache regime is read first.
_CACHE_ORDER = ("cold", "warm")


def _sort_key(row: dict) -> tuple:
    """Order rows by concurrency, then prefix-share, then cold before warm."""
    return (
        row["concurrency"],
        row["prefix_share"],
        _CACHE_ORDER.index(row["cache_state"]),
    )


def _require(record: dict, key: str, arm: str, source: object) -> object:
    """Read a field a joined record must carry, or fail with context.

    The arms are matched by source or segment, but a matched record can still
    lack the figure the row needs (a truncated tool output). A bare KeyError
    would hide which arm and run; this names both, as the file's other guards do.

    :param record: the joined arm record.
    :param key: the field the row needs from it.
    :param arm: which arm the record is, for the message.
    :param source: the run the row is being built for, for the message.
    :return: the field value.
    :raise ReportError: when the field is absent.
    """
    if key not in record:
        raise ReportError(f"{arm} record for {source!r} is missing {key}")
    return record[key]


def _build_row(
    record: dict, self_hosted_cost: list[dict], commercial_cost: list[dict]
) -> dict:
    """Join one prefix-cache record to its self-hosted and commercial arms."""
    request_rate = record.get("request_rate")
    prefix_share = record.get("prefix_share")
    cache_state = record.get("cache_state")
    source = record.get("source")
    # Both segment keys must ride on the record; without them the run cannot be
    # placed in a concurrency/prefix-share segment and the report would blend it.
    if request_rate is None:
        raise ReportError(f"prefix-cache record {source!r} has no request_rate")
    if prefix_share is None:
        raise ReportError(f"prefix-cache record {source!r} has no prefix_share")
    # The cold/warm label is the whole point of the report; an unlabelled run
    # would render and sort as a nothing, so reject it rather than pass it through.
    if cache_state not in _CACHE_ORDER:
        raise ReportError(
            f"prefix-cache record {source!r} has invalid cache_state "
            f"{cache_state!r}: want one of {_CACHE_ORDER}"
        )

    self_hosted = _match_by_source(self_hosted_cost, source)
    commercial = _match_by_segment(commercial_cost, request_rate, prefix_share)
    client_metrics = record.get("client_metrics") or {}
    return {
        "concurrency": request_rate,
        "prefix_share": prefix_share,
        "cache_state": cache_state,
        "slo": {key: client_metrics.get(key) for key in _SLO_KEYS},
        "self_hosted_usd_per_1m": {
            "input": _require(
                self_hosted, "cost_per_1m_input_usd", "self-hosted", source
            ),
            "output": _require(
                self_hosted, "cost_per_1m_output_usd", "self-hosted", source
            ),
        },
        "commercial_usd_per_1m": {
            "input": _require(
                commercial, "cost_per_1m_input_usd", "commercial", source
            ),
            "output": _require(
                commercial, "cost_per_1m_output_usd", "commercial", source
            ),
            "api": _require(commercial, "api", "commercial", source),
            "model": _require(commercial, "model", "commercial", source),
        },
    }


def _match_by_source(records: list[dict], source: object) -> dict:
    """Find the one cost record sharing this run's result-file source."""
    for candidate in records:
        if candidate.get("source") == source:
            return candidate
    raise ReportError(f"no self-hosted cost record for source {source!r}")


def _match_by_segment(
    records: list[dict], request_rate: object, prefix_share: object
) -> dict:
    """Find the commercial record for this run's (request_rate, prefix_share)."""
    for candidate in records:
        if (
            candidate.get("request_rate") == request_rate
            and candidate.get("prefix_share") == prefix_share
        ):
            return candidate
    raise ReportError(
        f"no commercial cost record for segment "
        f"request_rate={request_rate!r} prefix_share={prefix_share!r}"
    )


_COLUMNS = (
    "concurrency",
    "prefix-share",
    "cache",
    "self-hosted $/1M in",
    "self-hosted $/1M out",
    "commercial $/1M in",
    "commercial $/1M out",
    "p95 TTFT ms",
    "p99 TTFT ms",
    "p95 TPOT ms",
    "p99 TPOT ms",
    "goodput req/s",
)


def _usd(value: object) -> str:
    """Render a $/1M figure to cents, or a dash when the figure is null."""
    return f"{value:.2f}" if isinstance(value, (int, float)) else "-"


def _num(value: object) -> str:
    """Render an SLO number to one decimal, or a dash when unrecorded."""
    return f"{value:.1f}" if isinstance(value, (int, float)) else "-"


def _row_cells(row: dict) -> list[str]:
    """Flatten one report row into its Markdown cells, column order."""
    self_hosted = row["self_hosted_usd_per_1m"]
    commercial = row["commercial_usd_per_1m"]
    slo = row["slo"]
    return [
        str(row["concurrency"]),
        str(row["prefix_share"]),
        str(row["cache_state"]),
        _usd(self_hosted["input"]),
        _usd(self_hosted["output"]),
        _usd(commercial["input"]),
        _usd(commercial["output"]),
        _num(slo["p95_ttft_ms"]),
        _num(slo["p99_ttft_ms"]),
        _num(slo["p95_tpot_ms"]),
        _num(slo["p99_tpot_ms"]),
        _num(slo["request_goodput"]),
    ]


def render_markdown(rows: list[dict]) -> str:
    """Render report rows as a GitHub-flavored Markdown table.

    :param rows: the rows :func:`build_report` produced, already segmented and
        ordered.
    :return: the table as a single string: header, separator, one row per segment.
    """
    header = "| " + " | ".join(_COLUMNS) + " |"
    separator = "| " + " | ".join("---" for _ in _COLUMNS) + " |"
    body = ["| " + " | ".join(_row_cells(row)) + " |" for row in rows]
    return "\n".join([header, separator, *body])
