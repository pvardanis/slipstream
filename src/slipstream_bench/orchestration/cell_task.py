"""The resumable-cell primitive: one Prefect ``@task`` wrapping a single bench cell.

ADR-0012 (with its Amendment): the task wraps **one** cell — the grid loop lives in
the orchestration layer, the container executes the single cell it is handed. The task
runs the cell, gates the result through :func:`validate_cell`, and returns the cell's
S3 pointer; an invalid result raises out of the task so Prefect never caches a failure
and the next run re-attempts it. It is keyed ``digest:point-slug:cell-name`` via
:func:`get_cell_cache_key`, with ``result_storage`` and cache ``key_storage`` pointed at S3
so resume survives a server or laptop death.

Prefect is imported lazily inside :func:`build_cell_task`, so this module — and the
pure :func:`run_cell` core — import without the ``orchestration`` optional deps. Each
task is its own transaction (Prefect's default): callers **must not** wrap the sweep in
an enclosing ``transaction()``, which would defer every write to flow end and forfeit
per-cell resume.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slipstream_bench.orchestration import storage
from slipstream_bench.orchestration.cache_key import get_cell_cache_key
from slipstream_bench.orchestration.validity import (
    DEFAULT_MAX_ERROR_RATE,
    validate_cell,
)

if TYPE_CHECKING:
    from prefect import Task

# Runs the single cell it wraps — a ``docker run`` of one ``vllm bench serve`` — and
# raises on process failure. Stateless and single-op, so a typed Callable, not a
# Protocol; the orchestration layer names the port and the caller adapts to it.
CellExecution = Callable[[], None]


def run_cell(
    execute_func: CellExecution,
    *,
    result_path: Path,
    result_uri: str,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
) -> str:
    """Run one cell, gate its result, and return the cell's pointer.

    The Prefect-free core of the bench task: any raise here (an execution failure or
    an invalid result) leaves the task with no value to cache, so the cell re-attempts
    on the next run.

    :param execute_func: runs the single cell, raising on process failure.
    :param result_path: where the executed cell wrote its result JSON, gated before
        the pointer is returned.
    :param result_uri: the cell's S3 pointer, the task's return value and the single
        source of truth Prefect points at (never a competing copy of the numbers).
    :param max_error_rate: the health threshold passed to :func:`validate_cell`.
    :return: ``result_uri`` once the result passes the validity gate.
    :raise InvalidCellError: when the produced result is not a measurement.
    """
    execute_func()
    validate_cell(result_path, max_error_rate=max_error_rate)
    return result_uri


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
    return run_cell(execute_func, result_path=result_path, result_uri=result_uri)


def build_cell_task(
    *,
    result_storage: Any,
    key_storage: Any,
    retries: int = 0,
) -> "Task[..., str]":
    """Build the Prefect task wrapping one bench cell, keyed for per-cell resume.

    Prefect is imported here, not at module top, so the module stays importable
    without the orchestration optional deps. The cache policy is the cell's
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
    from prefect import task
    from prefect.cache_policies import CachePolicy

    policy = CachePolicy.from_cache_key_fn(get_cell_cache_key).configure(
        key_storage=key_storage
    )
    as_task = task(
        cache_policy=policy,
        result_storage=result_storage,
        persist_result=True,
        retries=retries,
    )
    return as_task(_bench_cell)


# The block names the two S3 storage blocks register under. Prefect resolves a
# task's storage from a saved block document, so each is persisted server-side under a
# stable name before the task binds it.
_RESULT_BLOCK = "sweep-cell-results"
_CACHE_KEY_BLOCK = "sweep-cell-cache-keys"


def cell_task(bucket: str, *, retries: int = 0) -> "Task[..., str]":
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
