"""The resumable-cell primitive: one Prefect ``@task`` wrapping a single bench cell.

ADR-0012 (with its Amendment): the task wraps **one** cell — the grid loop lives in
the orchestration layer, the container executes the single cell it is handed. The task
runs the cell, gates the result through :func:`validate_cell`, publishes the cell's
pointer and four headline metrics as a markdown artifact onto its own run, and returns
the cell's S3 pointer; an invalid result raises out of the task so Prefect never caches
a failure and the next run re-attempts it. It is keyed ``digest:point-slug:cell-name``
via :func:`get_cell_cache_key`, with ``result_storage`` and cache ``key_storage``
pointed at S3 so resume survives a server or laptop death.

The per-cell artifact is published from inside the task body, so it lands on the
sub-task's own Artifacts tab (``create_markdown_artifact`` binds the artifact to the
active run context, with no way to target another run's). A cache hit skips the body,
so a reused cell draws no new artifact — the run that measured it already carries one.

Each task is its own transaction (Prefect's default): callers **must not** wrap the
sweep in an enclosing ``transaction()``, which would defer every write to flow end and
forfeit per-cell resume.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prefect import Task, task
from prefect.artifacts import create_markdown_artifact
from prefect.cache_policies import CachePolicy

from slipstream_bench.contract import LoadCell, read_result
from slipstream_bench.orchestration import storage
from slipstream_bench.orchestration.cache_key import get_cell_cache_key
from slipstream_bench.orchestration.validity import (
    DEFAULT_MAX_ERROR_RATE,
    validate_cell,
)

# Runs the single cell it wraps — a ``docker run`` of one ``vllm bench serve`` — and
# raises on process failure. Stateless and single-op, so a typed Callable, not a
# Protocol; the orchestration layer names the port and the caller adapts to it.
CellExecution = Callable[[], None]

# Publishes one markdown artifact onto the running task, injected so run_cell's core
# stays Prefect-free and the publish path is covered against a fake. Production binds
# ``prefect.artifacts.create_markdown_artifact``.
ArtifactPublisher = Callable[..., Any]

# The heading the per-cell artifact carries on its sub-task's Artifacts tab.
_CELL_ARTIFACT_HEADING = "## cell result"

_LOGGER = logging.getLogger(__name__)

# The task-run name template Prefect fills from the task's ``point_slug`` and
# ``cell_name`` call parameters, so a run reads its grid coordinate — tier1 point slug,
# then tier2 cell name — in the run list instead of the bare function name.
_CELL_RUN_NAME = "{point_slug}:{cell_name}"

# The block names the two S3 storage blocks register under. Prefect resolves a
# task's storage from a saved block document, so each is persisted server-side under a
# stable name before the task binds it.
_RESULT_BLOCK = "sweep-cell-results"
_CACHE_KEY_BLOCK = "sweep-cell-cache-keys"


def cell_task(bucket: str, *, retries: int = 0) -> Task[..., str]:
    """Build the bench-cell task with its result and cache storage on S3.

    The production wiring of :func:`build_cell_task`: both storage blocks are rooted
    in the sweep results bucket, on the two distinct prefixes
    (:mod:`slipstream_bench.orchestration.storage`) so the cell pointer and the cache
    index never collide. Prefect binds a task's storage from a persisted block
    document, so each block is registered (idempotently, overwriting a prior
    definition) before the task binds it — this must run where the Prefect API is
    reachable, i.e. within the flow the sweep drives.

    :param bucket: the sweep results bucket (``RESULTS_BUCKET``).
    :param retries: opt-in retries for a flaky cell.
    :return: the configured task, its state on S3.
    :raise StorageError: when ``bucket`` is blank.
    """
    result = storage.result_storage(bucket)
    cache_keys = storage.cache_key_storage(bucket)
    result.save(_RESULT_BLOCK, overwrite=True)
    cache_keys.save(_CACHE_KEY_BLOCK, overwrite=True)
    return build_cell_task(
        result_storage=result, key_storage=cache_keys, retries=retries
    )


def build_cell_task(
    *,
    result_storage: Any,
    key_storage: Any,
    retries: int = 0,
) -> Task[..., str]:
    """Build the Prefect task wrapping one bench cell, keyed for per-cell resume.

    The cache policy is the cell's
    ``digest:point-slug:cell-name`` key; ``key_storage`` points the cache index and
    ``result_storage`` the persisted pointer at durable storage. Both are required —
    left to Prefect's local defaults, resume would silently degrade to the machine the
    task last ran on, the exact cross-machine failure ADR-0012 storage exists to
    prevent. Tests pass a tmp ``LocalFileSystem``; production passes S3 blocks.

    :param result_storage: the block Prefect persists the returned pointer into.
    :param key_storage: the block Prefect stores cache records into.
    :param retries: opt-in retries for a flaky cell (ADR-0012: retries are per
        invocation).
    :return: the configured ``@task`` — call it with ``digest``, ``point_slug``,
        ``cell_name``, ``execute_func``, ``result_path``, and ``result_uri``.
    """
    policy = CachePolicy.from_cache_key_fn(get_cell_cache_key).configure(
        key_storage=key_storage
    )
    as_task = task(
        task_run_name=_CELL_RUN_NAME,
        cache_policy=policy,
        result_storage=result_storage,
        persist_result=True,
        retries=retries,
    )
    return as_task(_bench_cell)


def run_cell(
    execute_func: CellExecution,
    *,
    result_path: Path,
    result_uri: str,
    publish: ArtifactPublisher,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
) -> str:
    """Run one cell, gate its result, publish its metrics, and return the cell's pointer.

    The Prefect-free core of the bench task: any raise here (an execution failure or
    an invalid result) leaves the task with no value to cache, so the cell re-attempts
    on the next run. Once the result passes the gate, its pointer and four headline
    metrics are published as an artifact onto the running task — so the sub-task reads
    its own result in the Prefect UI without opening S3. The publish runs only on a
    cache miss, the run that actually measures: a cache hit skips this body and reuses
    the pointer, so no new artifact is drawn for a cell already measured.

    :param execute_func: runs the single cell, raising on process failure.
    :param result_path: where the executed cell wrote its result JSON, gated before
        the pointer is returned and re-read for the artifact's metrics.
    :param result_uri: the cell's S3 pointer, the task's return value and the single
        source of truth Prefect points at (never a competing copy of the numbers).
    :param publish: the markdown-artifact publisher
        (:func:`prefect.artifacts.create_markdown_artifact` in production), called with
        ``key`` and ``markdown``.
    :param max_error_rate: the health threshold passed to :func:`validate_cell`.
    :return: ``result_uri`` once the result passes the validity gate.
    :raise InvalidCellError: when the produced result is not a measurement.
    """
    _LOGGER.info("cell %s starting", result_uri)
    execute_func()
    validate_cell(result_path, max_error_rate=max_error_rate)
    cell = LoadCell.from_record(read_result(result_path), result_path)
    _publish_cell_artifact(publish, cell, result_uri)
    _LOGGER.info(
        "cell %s done: goodput %.3f (p95 ttft %.0fms, p95 tpot %.0fms)",
        result_uri,
        cell.goodput_fraction,
        cell.p95_ttft_ms,
        cell.p95_tpot_ms,
    )
    return result_uri


def _publish_cell_artifact(
    publish: ArtifactPublisher, cell: LoadCell, result_uri: str
) -> None:
    """Publish the cell's artifact best-effort: drawing it must not void the cell.

    The measurement is already durable in S3 at ``result_uri`` and the artifact is a UI
    convenience, so a failure in the publish I/O — a transient Prefect API error — is logged
    with the cell's pointer and swallowed rather than raised. Raising would leave the task
    uncached and force a re-run of the expensive GPU benchmark (ADR-0012). The validity
    gate's own raise stays fatal: an invalid result *should* void the cache; a failed UI
    write should not. ``Exception`` is caught broadly on purpose — nothing in the publish
    call may outweigh a measured cell. The markdown is built *before* the try: rendering is
    pure formatting off the gated cell, so a defect there is a local code bug, not a UI-write
    failure, and must surface rather than be swallowed and mislabeled a publish failure.
    """
    markdown = _cell_artifact_markdown(cell, result_uri)
    try:
        publish(key=None, markdown=markdown)
    except Exception:
        _LOGGER.warning(
            "failed to publish the cell artifact for %s; the measurement is safe in S3, "
            "continuing",
            result_uri,
            exc_info=True,
        )


def _cell_artifact_markdown(cell: LoadCell, result_uri: str) -> str:
    """Render one cell's pointer and its four headline metrics as an artifact body.

    The S3 pointer is the single source of the numbers; the one-row table lifts the
    goodput fraction, the two p95 SLO gates (ttft prefill-bound, tpot decode-bound),
    and the output token rate onto the sub-task so the rung reads without opening S3.
    The gates round to whole milliseconds and the rate to one decimal, mirroring the
    aggregated tables (:mod:`slipstream_bench.report.chart`).
    """
    header = "| goodput_fraction | p95_ttft_ms | p95_tpot_ms | output_throughput |"
    separator = "| --- | --- | --- | --- |"
    row = (
        f"| {cell.goodput_fraction:.3f} | {cell.p95_ttft_ms:.0f} "
        f"| {cell.p95_tpot_ms:.0f} | {cell.output_throughput:.1f} |"
    )
    return (
        f"{_CELL_ARTIFACT_HEADING}\n\n`{result_uri}`\n\n{header}\n{separator}\n{row}\n"
    )


def _bench_cell(
    *,
    digest: str,
    point_slug: str,
    cell_name: str,
    execute_func: CellExecution,
    result_path: Path,
    result_uri: str,
) -> str:
    """Run one cell and return its pointer — the function Prefect wraps as a task.

    ``digest``, ``point_slug``, and ``cell_name`` are unread by this body: they address
    the cell, they do not steer its run. They are declared as parameters because Prefect
    hands :func:`get_cell_cache_key` only a task's call parameters, so a value can shape
    the cache key only by arriving as one — the key is built from the three before the
    body runs, then a hit skips the body entirely (ADR-0012:90-94).

    :param digest: the deep config digest, a cache-key part.
    :param point_slug: the engine-knob point slug, a cache-key part.
    :param cell_name: the client-load cell name, a cache-key part.
    :param execute_func: runs the single cell, raising on process failure.
    :param result_path: where the executed cell wrote its result JSON.
    :param result_uri: the cell's S3 pointer, the task's return value.
    :return: ``result_uri`` once the result passes the validity gate.
    :raise InvalidCellError: when the produced result is not a measurement.
    """
    return run_cell(
        execute_func,
        result_path=result_path,
        result_uri=result_uri,
        publish=create_markdown_artifact,
    )
