"""The redeploy-skip gate: are an engine point's cells all already valid in S3?

ADR-0015: before the ~20-minute GPU redeploy and the ceiling scrape, the parent knob
sweep asks whether a point still has cells to run. A point whose every cell already holds
a valid S3 measurement is skipped — its deploy and scrape are not re-paid to run zero
cells. This enumerates the point's cells from the grid (their single source, ADR-0012),
reads each cell's result object from S3, and runs the same validity gate a re-run reads
(:func:`slipstream_bench.orchestration.validity.is_cell_valid`), so "done" means the one
measurement check everywhere. An absent object, a transient network failure, or a
degenerate result counts as pending, so a point is never skipped on a cell it has not
actually measured. A failure re-running cannot fix — an access denial, a missing
credential, a wrong bucket — is not degraded to pending: it propagates and aborts the
sweep, so a misconfigured run fails at the first probe rather than re-running every
point's GPU redeploy against a bucket it can never read.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from botocore.exceptions import (
    ClientError,
    HTTPClientError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)
from slipstream_bench.contract import (
    EnginePoint,
    SweepGrid,
    build_point_sweep_config,
    get_cell_basename,
)

from slipstream_bench.orchestration.cell_run import get_cell_s3_key
from slipstream_bench.orchestration.validity import (
    DEFAULT_MAX_ERROR_RATE,
    is_cell_valid,
)

# The point's cells key the same way whatever endpoint or output dir they ran against, so
# enumerating them needs only non-empty placeholders for the two the addressing ignores.
_PLACEHOLDER_BASE_URL = "http://127.0.0.1:0"
_PLACEHOLDER_OUT_DIR = "/tmp"

_LOGGER = logging.getLogger(__name__)

# The S3 error codes that mean "the object is not there yet", the expected pending state
# for a cell a run has not written. Every other ClientError code (403 AccessDenied, a
# wrong-bucket NoSuchBucket) is a failure re-running cannot fix, so it is not degraded.
# download_file heads the object first, so a missing key surfaces as a 404, not NoSuchKey.
_ABSENT_OBJECT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


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
    :return: True when every cell holds a valid measurement, False on the first that is
        absent, transiently unreadable, or degenerate.
    :raise ClientError: on any S3 error other than an absent object (e.g. 403
        AccessDenied), so a misconfigured run fails loud rather than re-running forever.
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

    An absent object or a transient network failure is a pending cell, not a raise: the
    point is re-run rather than frozen on an object it never wrote. A failure re-running
    cannot fix — an access denial, a missing credential, a wrong bucket — propagates, so
    a misconfigured sweep aborts at the first probe instead of re-running every point.

    :param s3_client: the boto3 S3 client (or stand-in).
    :param bucket: the results bucket.
    :param key: the cell object's key under the bucket.
    :param dest: the local path the object downloads to.
    :param max_error_rate: the health threshold the validity gate applies.
    :return: True when the object exists and passes the gate, False when it is absent or
        a transient read failure leaves it pending.
    :raise ClientError: on any S3 error other than an absent object (e.g. 403
        AccessDenied) — re-running cannot fix it, so the sweep fails loud.
    """
    try:
        s3_client.download_file(bucket, key, str(dest))
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code not in _ABSENT_OBJECT_CODES:
            # A denial, a wrong bucket, a throttle past retries: re-running the point
            # reads the same failure forever, so fail the sweep loud instead of degrading.
            raise
        return False
    except (BotoConnectionError, HTTPClientError) as error:
        # A transient network failure — endpoint unreachable, a read timeout: the object
        # may well be there, so re-run the point, but log so a persistent one is visible.
        _LOGGER.warning(
            "cell object s3://%s/%s could not be read (%s): counting the cell pending, "
            "so the point re-runs",
            bucket,
            key,
            type(error).__name__,
        )
        return False
    return is_cell_valid(dest, max_error_rate=max_error_rate)
