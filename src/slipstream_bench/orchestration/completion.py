"""The redeploy-skip gate: are an engine point's cells all already valid in S3?

ADR-0015: before the ~20-minute GPU redeploy and the ceiling scrape, the parent knob
sweep asks whether a point still has cells to run. A point whose every cell already holds
a valid S3 measurement is skipped — its deploy and scrape are not re-paid to run zero
cells. This enumerates the point's cells from the grid (their single source, ADR-0012),
reads each cell's result object from S3, and runs the same validity gate a re-run reads
(:func:`slipstream_bench.orchestration.validity.is_cell_valid`), so "done" means the one
measurement check everywhere. A missing object, a broken download, or a degenerate result
counts as pending, so a point is never skipped on a cell it has not actually measured.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from slipstream_bench.orchestration.cell_run import get_cell_s3_key
from slipstream_bench.orchestration.validity import (
    DEFAULT_MAX_ERROR_RATE,
    is_cell_valid,
)
from slipstream_bench.sweep.aggregation import EnginePoint
from slipstream_bench.sweep.grid import SweepGrid, build_point_sweep_config
from slipstream_bench.sweep.runner import get_cell_basename

# The point's cells key the same way whatever endpoint or output dir they ran against, so
# enumerating them needs only non-empty placeholders for the two the addressing ignores.
_PLACEHOLDER_BASE_URL = "http://127.0.0.1:0"
_PLACEHOLDER_OUT_DIR = "/tmp"


def point_is_complete(
    point: EnginePoint,
    *,
    grid: SweepGrid,
    run_prefix: str,
    bucket: str,
    s3_client: Any,
    model: str,
    commercial: bool = False,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
) -> bool:
    """Report whether every one of a point's cells already holds a valid S3 result.

    Enumerates the point's cells from the grid, then reads each cell's result object under
    ``sweeps/<run_prefix>/<point-slug>/`` and runs the validity gate on it. Returns as
    soon as one cell is missing or invalid, so a partial point is cheap to probe.

    :param point: the engine-knob point being probed.
    :param grid: the validated grid the point's cells are enumerated from.
    :param run_prefix: the knob sweep's shared run id (the cells nest under
        ``<run_prefix>/<point-slug>``).
    :param bucket: the results bucket the cell objects live in.
    :param s3_client: the boto3 S3 client (or stand-in) objects are downloaded with.
    :param model: the served model id (folded into the point's sweep config).
    :param commercial: whether this is the commercial arm (matches the run's config).
    :param max_error_rate: the health threshold the validity gate applies.
    :return: True when every cell holds a valid measurement, False on the first that
        does not (missing, unreadable, or degenerate).
    """
    config = build_point_sweep_config(
        grid,
        point.slug(),
        base_url=_PLACEHOLDER_BASE_URL,
        model=model,
        out_dir=_PLACEHOLDER_OUT_DIR,
        commercial=commercial,
    )
    run_id = f"{run_prefix}/{point.slug()}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for cell in config.cells():
            key = get_cell_s3_key(run_id, cell)
            dest = tmp_dir / get_cell_basename(cell)
            if not _object_is_valid_cell(
                s3_client, bucket, key, dest, max_error_rate=max_error_rate
            ):
                return False
    return True


def _object_is_valid_cell(
    s3_client: Any,
    bucket: str,
    key: str,
    dest: Path,
    *,
    max_error_rate: float,
) -> bool:
    """Download one cell's result object and report whether it passes the validity gate.

    A download failure (a missing object, a transport error) is a pending cell, not a
    raise: the point is re-run rather than frozen on an object it never wrote.

    :param s3_client: the boto3 S3 client (or stand-in).
    :param bucket: the results bucket.
    :param key: the cell object's key under the bucket.
    :param dest: the local path the object downloads to.
    :param max_error_rate: the health threshold the validity gate applies.
    :return: True when the object exists and passes the gate, False otherwise.
    """
    try:
        s3_client.download_file(bucket, key, str(dest))
    # Any failure to obtain the object — a missing key, a transport or auth error — is a
    # pending cell, not a raise: an unattended sweep degrades to re-running the point
    # rather than aborting, and never false-completes a cell it could not read. Mirrors
    # is_cell_valid's own conservative contract (False on any validation failure).
    except Exception:  # noqa: BLE001
        return False
    return is_cell_valid(dest, max_error_rate=max_error_rate)
