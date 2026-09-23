"""Aggregate a knob-sweep run into the closed-loop concurrency ceiling per point.

Pure post-processing over the JSON the `just knob-sweep` recipe collects (ADR-0009):
one subdir per engine-knob point (mns{N}_kv{fp8|fp16}_pc{on|off}), each holding the
Tier-2 client JSONs `vllm bench serve` wrote across the --max-concurrency ladder.
The ceiling is the highest ladder rung still holding goodput >= 95% at the shared
SLO. Failed requests are cohorted {timeout, other} from the client JSON's per-request
errors (or the completed-short-of-attempted shortfall when a run saved no per-request
detail). oom needs the pod OOMKilled event plus an engine-log CUDA-OOM scrape, and
vLLM's num_preemptions soft-fail signal needs a /metrics snapshot — captures the
recipe does not collect, so both are surfaced as not-captured rather than invented.
Rows are keyed by (max-num-seqs, kv-cache-dtype, prefix-caching) with one row per
prefix-share within a point — the chart reads the engine knobs as x / series / facet
and prefix-share as the within-point dimension.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from slipstream_bench.results import read_result, to_numeric_metric

# A point subdir is mns{N}_kv{fp8|fp16}_pc{on|off} — the engine knobs one Tier-1
# redeploy was rendered with, encoded in the name the recipe nests its JSON under.
_POINT_PATTERN = re.compile(r"^mns(?P<mns>\d+)_kv(?P<kv>fp8|fp16)_pc(?P<pc>on|off)$")

# vLLM stores a failed request's error as the formatted exception traceback, so a
# client-side deadline miss (asyncio.TimeoutError) writes the type name — and thus
# this lowercased marker — into the error text. The match is a substring over the
# whole traceback, so any error text containing "timeout" counts as timeout; every
# other non-empty error is a hard failure of an unclassified kind.
_TIMEOUT_MARKER = "timeout"

# Goodput floor the ceiling is read off: a rung holds only if at least this
# fraction of its completed requests met both SLO thresholds (ADR-0009 §ceiling).
_GOODPUT_FLOOR = 0.95


class SweepAggregationError(Exception):
    """A sweep artifact that cannot be aggregated into a ceiling row."""


class FailureCohorts(TypedDict):
    """The failed-request cohorts summed across a ceiling row's rungs.

    ``oom`` stays ``None`` when the sweep collected no OOMKilled event and engine-log
    scrape — a not-captured cohort, distinct from a measured zero.
    """

    timeout: int
    other: int
    oom: int | None


class CeilingRow(TypedDict):
    """One folded row: a point-and-share ceiling with its summed failure cohorts.

    ``ceiling`` is ``None`` for a point-and-share that held no passing rung, and
    ``num_preemptions`` ``None`` when the sweep took no /metrics snapshot — both
    not-captured, never an invented zero (ADR-0009).
    """

    max_num_seqs: int
    kv_cache_dtype: str
    prefix_caching: bool
    prefix_share: int
    ceiling: int | None
    failures: FailureCohorts
    num_preemptions: int | None


class RungRow(TypedDict):
    """One unfolded row: a single ladder rung's goodput at its offered concurrency.

    The cliff before :class:`CeilingRow` folds each ladder to one number — the point
    knobs, the rung's prefix-share and ``max_concurrency``, and its goodput fraction.
    """

    max_num_seqs: int
    kv_cache_dtype: str
    prefix_caching: bool
    prefix_share: int
    max_concurrency: int
    goodput_fraction: float


@dataclass(frozen=True)
class EnginePoint:
    """The engine-knob point one Tier-1 redeploy measured.

    The three engine knobs the sweep varies per redeploy: the batch cap
    (max-num-seqs), the KV-cache dtype (fp8 committed, fp16 the counterfactual
    baseline), and whether prefix caching was on. Binding them into one value object
    keeps the aggregation key from travelling as three loose values a caller could
    transpose.
    """

    max_num_seqs: int
    kv_cache_dtype: str
    prefix_caching: bool

    @classmethod
    def from_dirname(cls, name: str) -> "EnginePoint":
        """Parse a point off its sweep subdir name.

        :param name: the subdir the recipe nested a point's JSON under, e.g.
            ``mns64_kvfp8_pcon``.
        :return: the engine-knob point it names.
        :raise SweepAggregationError: when the name is not a valid point — a stray
            directory would otherwise aggregate as a nonsense key.
        """
        match = _POINT_PATTERN.match(name)
        if match is None:
            raise SweepAggregationError(
                f"'{name}' is not a knob-sweep point subdir "
                f"(want mns<N>_kv<fp8|fp16>_pc<on|off>)"
            )
        return cls(
            max_num_seqs=int(match["mns"]),
            kv_cache_dtype=match["kv"],
            prefix_caching=match["pc"] == "on",
        )

    def slug(self) -> str:
        """Name the subdir this point's Tier-2 JSON is nested under.

        The inverse of ``from_dirname``: the single builder of the
        ``mns{N}_kv{dtype}_pc{on|off}`` format the grid emits, the recipe writes,
        and the aggregator parses — so all three read one format from one place.

        :return: the point's subdir name, e.g. ``mns64_kvfp8_pcon``.
        """
        caching = "on" if self.prefix_caching else "off"
        return f"mns{self.max_num_seqs}_kv{self.kv_cache_dtype}_pc{caching}"


def goodput_fraction(record: dict, source: Path) -> float:
    """Read the fraction of a cell's completed requests that met the SLO.

    vLLM reports goodput and throughput as rates (req/s) over the same run window,
    so their ratio is the fraction of completed requests meeting both the ttft and
    tpot thresholds — the number the 95% ceiling test compares against. A cell that
    completed nothing has zero throughput and held no goodput, so its fraction is
    0.0 rather than an undefined 0/0.

    :param record: the cell's parsed ``vllm bench serve --save-result`` record.
    :param source: the cell's result file, for the error message.
    :return: the goodput fraction, 0.0 when the cell completed nothing.
    :raise SweepAggregationError: when either rate is absent, null, non-numeric,
        non-finite, or negative.
    """
    goodput = to_numeric_metric(
        record, source, "request_goodput", error_cls=SweepAggregationError
    )
    throughput = to_numeric_metric(
        record, source, "request_throughput", error_cls=SweepAggregationError
    )
    if throughput == 0:
        return 0.0
    return goodput / throughput


def classify_failures(record: dict, source: Path) -> dict:
    """Cohort a cell's failed requests into {timeout, oom, other} (ADR-0009).

    ``--save-detailed`` records one error string per request (empty on success), so
    a present ``errors`` list is split by matching a deadline marker to *timeout*
    and sending any other non-empty error to *other*. A result written without
    ``--save-detailed`` carries no per-request errors, so its failures — completed
    short of attempted — fall to *other*, their kind unknown. *oom* is always
    ``None``: it needs the pod ``OOMKilled`` event and engine-log scrape the sweep
    recipe does not collect, and an invented zero would read as measured-and-none.

    :param record: the cell's parsed result record.
    :param source: the cell's result file, for the error message.
    :return: the cohort counts ``{"timeout": int, "other": int, "oom": None}``.
    :raise SweepAggregationError: when ``errors`` is present but not a list, or holds a
        non-string entry — a malformed field cannot be cohorted.
    """
    errors = record.get("errors")
    if errors is None:
        return _get_cohorts_from_shortfall(record, source)
    if not isinstance(errors, list):
        raise SweepAggregationError(
            f"result {source} has a non-list errors field: cannot cohort failures"
        )
    if any(entry and not isinstance(entry, str) for entry in errors):
        raise SweepAggregationError(
            f"result {source} has a non-string errors entry: cannot cohort failures"
        )
    timeout = sum(1 for error in errors if error and _TIMEOUT_MARKER in error.lower())
    other = sum(1 for error in errors if error) - timeout
    return {"timeout": timeout, "other": other, "oom": None}


def _get_cohorts_from_shortfall(record: dict, source: Path) -> dict:
    """Cohort failures a no-detail result only knows as attempted-minus-completed.

    Without a per-request ``errors`` array the kind of each failure is unknown, so
    the shortfall of completed requests below the prompts attempted is charged to
    *other* — never silently dropped, never guessed as timeouts.

    :param record: the cell's parsed result record.
    :param source: the cell's result file, for the error message.
    :return: the cohort counts, the whole shortfall in *other*.
    :raise SweepAggregationError: when either count is absent, null, or not a whole
        number, or completed exceeds attempted — a broken result must not read as a
        clean zero-failure cell.
    """
    attempted = _require_int(record, source, "num_prompts")
    completed = _require_int(record, source, "completed")
    if completed > attempted:
        raise SweepAggregationError(
            f"result {source} completed {completed} of {attempted} attempted: "
            f"inconsistent counts"
        )
    return {"timeout": 0, "other": attempted - completed, "oom": None}


@dataclass(frozen=True)
class LoadCell:
    """One Tier-2 client-load ladder rung: an offered concurrency and how it held up.

    The two client knobs the ladder varies without a redeploy — the closed-loop
    ``--max-concurrency`` the rung offered and the prefix-share it ran — plus the
    fraction of its completed requests that met the SLO and its failure cohorts.
    """

    max_concurrency: int
    prefix_share: int
    goodput_fraction: float
    failures: dict

    @classmethod
    def from_record(cls, record: dict, source: Path) -> "LoadCell":
        """Build a cell from a parsed client JSON, validating each field at the seam.

        :param record: the cell's parsed ``vllm bench serve --save-result`` record.
        :param source: the cell's result file, for the error message.
        :return: the cell's offered concurrency, prefix-share, goodput fraction, and
            failure cohorts.
        :raise SweepAggregationError: when the cell lacks its closed-loop cap or its
            stamped prefix-share, or carries a bad goodput or errors field.
        """
        return cls(
            max_concurrency=_require_int(record, source, "max_concurrency"),
            prefix_share=_require_int(record, source, "prefix_share"),
            goodput_fraction=goodput_fraction(record, source),
            failures=classify_failures(record, source),
        )


def _require_int(record: dict, source: Path, key: str) -> int:
    """Read a whole-number join key a ladder cell must carry.

    :param record: the cell's parsed result record.
    :param source: the cell's result file, for the error message.
    :param key: the field to read as an int.
    :return: the field as an int.
    :raise SweepAggregationError: when the field is absent, null, or not a whole number
        — bool is an int subclass but never a valid cap or share.
    """
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SweepAggregationError(f"result {source} missing or non-integer {key}")
    return value


def read_cell(path: Path) -> LoadCell:
    """Read one Tier-2 client JSON into a ladder cell.

    :param path: the cell's ``vllm bench serve --save-result`` JSON.
    :return: the cell built and validated from the file's record.
    :raise SweepAggregationError: when the cell lacks its closed-loop cap or its stamped
        prefix-share, or carries a bad goodput or errors field.
    :raise ResultError: when the file cannot be read (see
        :func:`slipstream_bench.results.read_result`).
    """
    return LoadCell.from_record(read_result(path), path)


def get_ceiling(cells: list[LoadCell]) -> int | None:
    """Return the highest offered concurrency whose goodput held at the SLO.

    The ceiling is the highest ``--max-concurrency`` rung meeting the 95% goodput
    floor (ADR-0009). Taking the highest passing rung — not the last before the
    first dip — keeps a single noisy rung from truncating the ceiling early.

    :param cells: the ladder rungs for one point-and-share group.
    :return: the highest offered concurrency at or above the floor, or None when no
        rung held (or none was measured) — never a zero that reads as a real rung.
    """
    passing = [
        cell.max_concurrency
        for cell in cells
        if cell.goodput_fraction >= _GOODPUT_FLOOR
    ]
    return max(passing) if passing else None


def _get_point_dirs(run_dir: Path) -> list[tuple[EnginePoint, Path]]:
    """Find the engine-knob point subdirs under a run directory, in key order.

    A run directory also holds the predicted-ceilings ledger and a charts subdir;
    only the mns/kv/pc subdirs are points, so anything else is skipped rather than
    read as a cell source.

    :param run_dir: the ``bench/results/<run_id>`` directory the sweep wrote.
    :return: the (point, subdir) pairs, sorted by point key.
    """
    found = [
        (EnginePoint.from_dirname(child.name), child)
        for child in run_dir.iterdir()
        if child.is_dir() and _POINT_PATTERN.match(child.name)
    ]
    return sorted(found, key=lambda pair: _get_point_key(pair[0]))


def _get_point_key(point: EnginePoint) -> tuple[int, str, bool]:
    """Order points by max-num-seqs, then kv-dtype, then prefix-caching."""
    return (point.max_num_seqs, point.kv_cache_dtype, point.prefix_caching)


def _read_point_cells(subdir: Path) -> list[LoadCell]:
    """Read a point subdir's Tier-2 ladder rungs, failing fast when it holds none.

    :param subdir: the point's subdir of Tier-2 client JSONs.
    :return: the ladder cells the subdir holds, one per client JSON.
    :raise SweepAggregationError: when the subdir holds no ladder rungs — a point that
        measured nothing must not silently drop from the table.
    """
    cells = [read_cell(result) for result in sorted(subdir.glob("*.json"))]
    if not cells:
        raise SweepAggregationError(
            f"knob-sweep point {subdir} holds no ladder rungs (no *.json cells): "
            f"the point measured nothing"
        )
    return cells


def _get_rows_for_point(point: EnginePoint, subdir: Path) -> list[CeilingRow]:
    """Fold one point's ladder cells into a ceiling row per prefix-share.

    :param point: the engine-knob point the subdir measured.
    :param subdir: the point's subdir of Tier-2 client JSONs.
    :return: one row per prefix-share the point ran, in ascending share order.
    :raise SweepAggregationError: when the subdir holds no ladder rungs — a point that
        measured nothing must not silently drop from the table.
    """
    by_share: dict[int, list[LoadCell]] = defaultdict(list)
    for cell in _read_point_cells(subdir):
        by_share[cell.prefix_share].append(cell)
    return [_get_point_row(point, share, by_share[share]) for share in sorted(by_share)]


def _get_rungs_for_point(point: EnginePoint, subdir: Path) -> list[RungRow]:
    """Unfold one point's ladder cells into a per-rung row, the cliff before the fold.

    :param point: the engine-knob point the subdir measured.
    :param subdir: the point's subdir of Tier-2 client JSONs.
    :return: one row per rung, sorted by prefix-share then offered concurrency, each
        carrying the point knobs and the rung's own goodput fraction.
    :raise SweepAggregationError: when the subdir holds no ladder rungs.
    """
    cells = sorted(
        _read_point_cells(subdir),
        key=lambda cell: (cell.prefix_share, cell.max_concurrency),
    )
    return [_get_rung_row(point, cell) for cell in cells]


def _get_rung_row(point: EnginePoint, cell: LoadCell) -> RungRow:
    """Build one per-rung row: the point knobs, the rung's share, cap, and goodput."""
    return {
        "max_num_seqs": point.max_num_seqs,
        "kv_cache_dtype": point.kv_cache_dtype,
        "prefix_caching": point.prefix_caching,
        "prefix_share": cell.prefix_share,
        "max_concurrency": cell.max_concurrency,
        "goodput_fraction": cell.goodput_fraction,
    }


def _get_point_row(point: EnginePoint, share: int, cells: list[LoadCell]) -> CeilingRow:
    """Build one ceiling row from a point-and-share group's ladder cells.

    The measured ceiling and the summed failure cohorts across the group's rungs.
    ``oom`` and ``num_preemptions`` stay ``None``: oom needs the pod OOMKilled event
    and engine-log scrape, num_preemptions a /metrics snapshot — neither collected by
    the sweep recipe (ADR-0009), and an invented zero would read as measured-and-none.
    """
    return {
        "max_num_seqs": point.max_num_seqs,
        "kv_cache_dtype": point.kv_cache_dtype,
        "prefix_caching": point.prefix_caching,
        "prefix_share": share,
        "ceiling": get_ceiling(cells),
        "failures": {
            "timeout": sum(cell.failures["timeout"] for cell in cells),
            "other": sum(cell.failures["other"] for cell in cells),
            "oom": None,
        },
        "num_preemptions": None,
    }


def aggregate_ceilings(run_dir: Path) -> list[CeilingRow]:
    """Fold a knob-sweep run into ceiling rows, one per point and prefix-share.

    :param run_dir: the ``bench/results/<run_id>`` directory the sweep wrote, one
        subdir per engine-knob point.
    :return: the ceiling rows, sorted by point key then prefix-share.
    :raise SweepAggregationError: when the directory holds no point subdirs, or a point
        subdir holds no ladder rungs (an empty run or point measured nothing and must
        not report zero rows as a clean result), or a cell cannot be aggregated — a
        missing cap or share, a bad goodput, or a malformed errors field (see
        :func:`read_cell`).
    :raise ResultError: when a cell file cannot be read (see :func:`read_cell`).
    """
    points = _require_point_dirs(run_dir)
    return [
        row for point, subdir in points for row in _get_rows_for_point(point, subdir)
    ]


def aggregate_rungs(run_dir: Path) -> list[RungRow]:
    """Unfold a knob-sweep run into per-rung rows, the goodput cliff behind the ceiling.

    Where :func:`aggregate_ceilings` folds each point-and-share ladder to one ceiling,
    this keeps every rung — the goodput fraction at each offered ``--max-concurrency`` —
    so the diagnostic chart plots the cliff the ceiling was read off (ADR-0009).

    :param run_dir: the ``bench/results/<run_id>`` directory the sweep wrote, one
        subdir per engine-knob point.
    :return: the rung rows, sorted by point key, then prefix-share, then offered
        concurrency.
    :raise SweepAggregationError: when the directory holds no point subdirs, or a point
        subdir holds no ladder rungs, or a cell cannot be aggregated (see
        :func:`read_cell`).
    :raise ResultError: when a cell file cannot be read (see :func:`read_cell`).
    """
    points = _require_point_dirs(run_dir)
    return [
        row for point, subdir in points for row in _get_rungs_for_point(point, subdir)
    ]


def _require_point_dirs(run_dir: Path) -> list[tuple[EnginePoint, Path]]:
    """Find a run's engine-knob point subdirs, failing fast when it holds none.

    :param run_dir: the ``bench/results/<run_id>`` directory the sweep wrote.
    :return: the (point, subdir) pairs, sorted by point key.
    :raise SweepAggregationError: when the directory holds no point subdirs — an empty
        run measured nothing and must not report zero rows as a clean result.
    """
    points = _get_point_dirs(run_dir)
    if not points:
        raise SweepAggregationError(
            f"no knob-sweep points under {run_dir} "
            f"(want mns<N>_kv<fp8|fp16>_pc<on|off> subdirs)"
        )
    return points
