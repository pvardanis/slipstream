"""Aggregate a knob-sweep run into the closed-loop concurrency ceiling per point.

Pure post-processing over the JSON the `just knob-sweep` recipe collects (ADR-0009):
one subdir per engine-knob point (mns{N}_kv{fp8|fp16}_pc{on|off}), each holding the
Tier-2 client JSONs `vllm bench serve` wrote across the --max-concurrency ladder.
The ceiling is the highest ladder rung still holding goodput >= 95% at the shared
SLO. Failed requests are cohorted {timeout, other} from the client JSON's per-request
errors (or the completed-short-of-attempted shortfall when a run saved no per-request
detail). oom needs the pod OOMKilled event plus an engine-log CUDA-OOM scrape, and
vLLM's num_preemptions soft-fail signal needs a /metrics snapshot — captures the
recipe does not collect yet, so both are surfaced as not-captured rather than
invented. Keyed by (max-num-seqs, kv-cache-dtype, prefix-caching), the
key the chart's x / series / facet read — a different key and output from report.py's
single-config cost economics join.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from slipstream_bench.results import read_result, to_numeric_metric

# A point subdir is mns{N}_kv{fp8|fp16}_pc{on|off} — the engine knobs one Tier-1
# redeploy was rendered with, encoded in the name the recipe nests its JSON under.
_POINT_PATTERN = re.compile(r"^mns(?P<mns>\d+)_kv(?P<kv>fp8|fp16)_pc(?P<pc>on|off)$")

# vLLM stores a failed request's error as the formatted exception traceback, so a
# client-side deadline miss (asyncio.TimeoutError) writes the type name — and thus
# this lowercased marker — into the error text. Any other non-empty error is a hard
# failure of an unclassified kind.
_TIMEOUT_MARKER = "timeout"

# Goodput floor the ceiling is read off: a rung holds only if at least this
# fraction of its completed requests met both SLO thresholds (ADR-0009 §ceiling).
_GOODPUT_FLOOR = 0.95


class SweepCeilingError(Exception):
    """A sweep artifact that cannot be aggregated into a ceiling row."""


@dataclass(frozen=True)
class Point:
    """The engine-knob point one Tier-1 redeploy measured.

    The three knobs the sweep varies per redeploy: the batch cap (max-num-seqs),
    the KV-cache dtype (fp8 committed, fp16 the counterfactual baseline), and
    whether prefix caching was on. Binding them into one value object keeps the
    aggregation key from travelling as three loose values a caller could transpose.
    """

    max_num_seqs: int
    kv_cache_dtype: str
    prefix_caching: bool

    @classmethod
    def from_dirname(cls, name: str) -> "Point":
        """Parse a point off its sweep subdir name.

        :param name: the subdir the recipe nested a point's JSON under, e.g.
            ``mns64_kvfp8_pcon``.
        :return: the engine-knob point it names.
        :raise SweepCeilingError: when the name is not a valid point — a stray
            directory would otherwise aggregate as a nonsense key.
        """
        match = _POINT_PATTERN.match(name)
        if match is None:
            raise SweepCeilingError(
                f"'{name}' is not a knob-sweep point subdir "
                f"(want mns<N>_kv<fp8|fp16>_pc<on|off>)"
            )
        return cls(
            max_num_seqs=int(match["mns"]),
            kv_cache_dtype=match["kv"],
            prefix_caching=match["pc"] == "on",
        )


def goodput_fraction(record: dict, source: Path) -> float:
    """Read the fraction of a cell's completed requests that met the SLO.

    vLLM reports goodput and throughput as rates (req/s) over the same run window,
    so their ratio is the fraction of completed requests meeting both the ttft and
    tpot thresholds — the number the 95% ceiling test compares against. A cell that
    completed nothing has zero throughput and held no goodput, so its fraction is
    0.0 rather than an undefined 0/0.

    :param record: the cell's parsed ``vllm bench serve --save-result`` record.
    :param source: the cell's result file, for the error message.
    :return: the goodput fraction in 0..1.
    :raise SweepCeilingError: when either rate is absent, null, non-numeric,
        non-finite, or negative.
    """
    goodput = to_numeric_metric(
        record, source, "request_goodput", error_cls=SweepCeilingError
    )
    throughput = to_numeric_metric(
        record, source, "request_throughput", error_cls=SweepCeilingError
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
    recipe does not collect yet, and an invented zero would read as measured-and-none.

    :param record: the cell's parsed result record.
    :param source: the cell's result file, for the error message.
    :return: the cohort counts ``{"timeout": int, "other": int, "oom": None}``.
    :raise SweepCeilingError: when ``errors`` is present but not a list, or holds a
        non-string entry — a malformed field cannot be cohorted.
    """
    errors = record.get("errors")
    if errors is None:
        return _cohorts_from_shortfall(record)
    if not isinstance(errors, list):
        raise SweepCeilingError(
            f"result {source} has a non-list errors field: cannot cohort failures"
        )
    if any(entry and not isinstance(entry, str) for entry in errors):
        raise SweepCeilingError(
            f"result {source} has a non-string errors entry: cannot cohort failures"
        )
    timeout = sum(1 for error in errors if error and _TIMEOUT_MARKER in error.lower())
    other = sum(1 for error in errors if error) - timeout
    return {"timeout": timeout, "other": other, "oom": None}


def _cohorts_from_shortfall(record: dict) -> dict:
    """Cohort failures a no-detail result only knows as attempted-minus-completed.

    Without a per-request ``errors`` array the kind of each failure is unknown, so
    the shortfall of completed requests below the prompts attempted is charged to
    *other* — never silently dropped, never guessed as timeouts.

    :param record: the cell's parsed result record.
    :return: the cohort counts, the whole shortfall in *other*.
    """
    attempted = record.get("num_prompts") or 0
    completed = record.get("completed") or 0
    return {"timeout": 0, "other": max(0, attempted - completed), "oom": None}


@dataclass(frozen=True)
class Cell:
    """One Tier-2 ladder rung: an offered concurrency and how it held up.

    The closed-loop ``--max-concurrency`` the rung offered, the prefix-share it ran,
    the fraction of its completed requests that met the SLO, and its failure cohorts.
    """

    max_concurrency: int
    prefix_share: int
    goodput_fraction: float
    failures: dict


def _require_int(record: dict, source: Path, key: str) -> int:
    """Read a whole-number join key a ladder cell must carry.

    :param record: the cell's parsed result record.
    :param source: the cell's result file, for the error message.
    :param key: the field to read as an int.
    :return: the field as an int.
    :raise SweepCeilingError: when the field is absent, null, or not a whole number
        — bool is an int subclass but never a valid cap or share.
    """
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SweepCeilingError(f"result {source} missing or non-integer {key}")
    return value


def read_cell(path: Path) -> Cell:
    """Read one Tier-2 client JSON into a ladder cell.

    :param path: the cell's ``vllm bench serve --save-result`` JSON.
    :return: the cell's offered concurrency, prefix-share, goodput fraction, and
        failure cohorts.
    :raise SweepCeilingError: when the cell lacks its closed-loop cap or its stamped
        prefix-share, or carries a bad goodput or errors field.
    :raise ResultError: when the file cannot be read (see
        :func:`slipstream_bench.results.read_result`).
    """
    record = read_result(path)
    return Cell(
        max_concurrency=_require_int(record, path, "max_concurrency"),
        prefix_share=_require_int(record, path, "prefix_share"),
        goodput_fraction=goodput_fraction(record, path),
        failures=classify_failures(record, path),
    )


def ceiling(cells: list[Cell]) -> int | None:
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


def _point_dirs(run_dir: Path) -> list[tuple[Point, Path]]:
    """Find the engine-knob point subdirs under a run directory, in key order.

    A run directory also holds the predicted-ceilings ledger and a charts subdir;
    only the mns/kv/pc subdirs are points, so anything else is skipped rather than
    read as a cell source.

    :param run_dir: the ``bench/results/<run_id>`` directory the sweep wrote.
    :return: the (point, subdir) pairs, sorted by point key.
    """
    found = [
        (Point.from_dirname(child.name), child)
        for child in run_dir.iterdir()
        if child.is_dir() and _POINT_PATTERN.match(child.name)
    ]
    return sorted(found, key=lambda pair: _point_key(pair[0]))


def _point_key(point: Point) -> tuple[int, str, bool]:
    """Order points by max-num-seqs, then kv-dtype, then prefix-caching."""
    return (point.max_num_seqs, point.kv_cache_dtype, point.prefix_caching)


def _rows_for_point(point: Point, subdir: Path) -> list[dict]:
    """Fold one point's ladder cells into a ceiling row per prefix-share.

    :param point: the engine-knob point the subdir measured.
    :param subdir: the point's subdir of Tier-2 client JSONs.
    :return: one row per prefix-share the point ran, in ascending share order.
    """
    by_share: dict[int, list[Cell]] = defaultdict(list)
    for result in sorted(subdir.glob("*.json")):
        cell = read_cell(result)
        by_share[cell.prefix_share].append(cell)
    return [_point_row(point, share, by_share[share]) for share in sorted(by_share)]


def _point_row(point: Point, share: int, cells: list[Cell]) -> dict:
    """Build one ceiling row from a point-and-share group's ladder cells.

    The measured ceiling and the summed failure cohorts across the group's rungs.
    ``oom`` and ``num_preemptions`` stay ``None``: oom needs the pod OOMKilled event
    and engine-log scrape, num_preemptions a /metrics snapshot — neither collected by
    the sweep recipe yet (ADR-0009), and an invented zero would read as measured-and-none.
    """
    return {
        "max_num_seqs": point.max_num_seqs,
        "kv_cache_dtype": point.kv_cache_dtype,
        "prefix_caching": point.prefix_caching,
        "prefix_share": share,
        "ceiling": ceiling(cells),
        "failures": {
            "timeout": sum(cell.failures["timeout"] for cell in cells),
            "other": sum(cell.failures["other"] for cell in cells),
            "oom": None,
        },
        "num_preemptions": None,
    }


def aggregate(run_dir: Path) -> list[dict]:
    """Fold a knob-sweep run into ceiling rows, one per point and prefix-share.

    :param run_dir: the ``bench/results/<run_id>`` directory the sweep wrote, one
        subdir per engine-knob point.
    :return: the ceiling rows, sorted by point key then prefix-share.
    :raise SweepCeilingError: when the directory holds no point subdirs (an empty run
        measured nothing and must not report zero rows as a clean result), or a cell
        cannot be aggregated — a missing cap or share, a bad goodput, or a malformed
        errors field (see :func:`read_cell`).
    :raise ResultError: when a cell file cannot be read (see :func:`read_cell`).
    """
    points = _point_dirs(run_dir)
    if not points:
        raise SweepCeilingError(
            f"no knob-sweep points under {run_dir} "
            f"(want mns<N>_kv<fp8|fp16>_pc<on|off> subdirs)"
        )
    return [row for point, subdir in points for row in _rows_for_point(point, subdir)]
