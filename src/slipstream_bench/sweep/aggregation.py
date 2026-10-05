"""Aggregate a knob-sweep run into the closed-loop concurrency ceiling per point.

Pure post-processing over the JSON the `just knob-sweep` recipe collects (ADR-0009):
one subdir per engine-knob point (mns{N}_kv{fp8|fp16}_pc{on|off}), each holding the
Tier-2 client JSONs `vllm bench serve` wrote across the --max-concurrency ladder.
The ceiling is the highest ladder rung still holding goodput >= 95% at the shared
SLO. The per-record value objects (:class:`EnginePoint`, :class:`LoadCell`) and their
parse are the contract kernel (:mod:`slipstream.contract.records`); this folds the
multi-cell run over them into rows keyed by (max-num-seqs, kv-cache-dtype,
prefix-caching) with one row per prefix-share within a point — the chart reads the
engine knobs as x / series / facet and prefix-share as the within-point dimension.
"""

from collections import defaultdict
from pathlib import Path
from typing import TypedDict

from slipstream.contract.records import (
    EnginePoint,
    FailureCohorts,
    LoadCell,
    SweepAggregationError,
)
from slipstream.contract.results import read_result

# Goodput floor the ceiling is read off: a rung holds only if at least this
# fraction of its completed requests met both SLO thresholds (ADR-0009 §ceiling).
_GOODPUT_FLOOR = 0.95


class CeilingScrapeError(Exception):
    """No concurrency ceiling could be scraped for an engine point.

    The parent knob sweep scrapes each redeployed engine's reported ceiling before
    running the point's Tier-2 ladder (ADR-0015). A scrape that finds none raises this
    rather than returning empty, so the point fails loudly instead of measuring its
    ladder against a garbage ceiling — the one failure an unattended sweep cannot
    tolerate.
    """


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


def read_cell(path: Path) -> LoadCell:
    """Read one Tier-2 client JSON into a ladder cell.

    :param path: the cell's ``vllm bench serve --save-result`` JSON.
    :return: the cell built and validated from the file's record.
    :raise SweepAggregationError: when the cell lacks its closed-loop cap or its stamped
        prefix-share, or carries a bad goodput or errors field.
    :raise ResultError: when the file cannot be read (see
        :func:`slipstream.contract.results.read_result`).
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
        if child.is_dir() and EnginePoint.is_point_dirname(child.name)
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
