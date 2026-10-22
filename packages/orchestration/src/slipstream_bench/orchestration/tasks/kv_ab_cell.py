"""The kv-cache-extension-ab cell task: one ladder rung measured over its prefix-cache window.

A rung reads vLLM's /metrics just before and just after its cell, so the prefix-cache hit
rates it labels are the counters' advance over the cell's own traffic rather than the
engine's lifetime ratio, which the warmup and earlier rungs pollute. It gates the result
through :func:`validate_cell`, labels the cell with the arm, run, and window
(:func:`build_kv_ab_labels`), writes those labels beside the raw result the bench host
stored, and returns the labels' S3 pointer. An invalid cell or an unreadable window raises
before the labels are written, so the task caches nothing and the next run re-measures
the rung.

The task is the knob sweep's cell task around this body: the same S3 storage blocks and
the same ``run-id:digest:point-slug:cell-name`` key, whose run id nests the A/B under
``kv-offload-ab/`` and whose point slug names the arm run (``<arm>-r<n>``).
"""

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from prefect import Task
from prefect.artifacts import create_markdown_artifact

from slipstream_bench.contract.kv_ab_record import Arm, build_kv_ab_labels
from slipstream_bench.contract.prefix_cache_window import get_prefix_cache_window
from slipstream_bench.orchestration.kv_ab_cell import MetricsError
from slipstream_bench.orchestration.tasks.cell import (
    ArtifactPublisher,
    CellExecution,
    build_cell_task,
    cell_task,
    report_cell,
)
from slipstream_bench.orchestration.validity import validate_cell

# Reads the serving engine's Prometheus /metrics exposition text.
MetricsReader = Callable[[], str]

# Writes a rung's labels and returns their S3 pointer.
LabelsWriter = Callable[[Mapping[str, Any]], str]

_LOGGER = logging.getLogger(__name__)


def kv_ab_cell_task(bucket: str, *, retries: int = 0) -> Task[..., str]:
    """Build the kv-ab cell task with its result and cache storage on S3.

    :param bucket: the results bucket (``RESULTS_BUCKET``).
    :param retries: opt-in retries for a flaky cell.
    :return: the configured task, its state on S3.
    :raise StorageError: when ``bucket`` is blank, or the cache-key storage fails its
        write/read probe.
    """
    return cell_task(bucket, retries=retries, body=_kv_ab_cell)


def build_kv_ab_cell_task(
    *, result_storage: Any, key_storage: Any, retries: int = 0
) -> Task[..., str]:
    """Build the kv-ab cell task over the given result and cache-key storage.

    :param result_storage: the block Prefect persists the returned pointer into.
    :param key_storage: the block Prefect stores cache records into.
    :param retries: opt-in retries for a flaky cell.
    :return: the configured ``@task``, called with ``run_id``, ``digest``,
        ``point_slug``, ``cell_name``, and :func:`run_kv_ab_cell`'s arguments.
    """
    return build_cell_task(
        result_storage=result_storage,
        key_storage=key_storage,
        retries=retries,
        body=_kv_ab_cell,
    )


def run_kv_ab_cell(
    execute_func: CellExecution,
    *,
    read_metrics: MetricsReader,
    result_path: Path,
    arm: Arm,
    run: int,
    model: str,
    result_uri: str,
    write_labels: LabelsWriter,
    publish: ArtifactPublisher,
) -> str:
    """Measure one rung over its prefix-cache window and write its labels.

    :param execute_func: runs the single cell, raising on process failure.
    :param read_metrics: reads the serving engine's /metrics, before and after the cell.
    :param result_path: where the executed cell wrote its result JSON.
    :param arm: the arm the rung is measured in.
    :param run: the run's 1-based repetition within its arm.
    :param model: the served model id, the ``model_name`` label the counters carry.
    :param result_uri: the raw result's ``s3://`` pointer, which the cell's artifact links.
    :param write_labels: writes the labels and returns their S3 pointer.
    :param publish: the markdown-artifact publisher for the rung's metrics artifact.
    :return: the labels' S3 pointer.
    :raise InvalidCellError: when the result is not a measurement.
    :raise PrefixCacheError: when the snapshots yield no window, e.g. counters that went
        backwards because the engine restarted mid-cell.
    :raise KvAbRecordError: when an off-arm cell's KV connector counters advanced, or an
        on-arm cell's KV connector queries did not.
    :raise MetricsError: when /metrics cannot be read; after the cell, the error names
        the cell that ran, which the next run re-measures.
    """
    before = read_metrics()
    execute_func()
    try:
        after = read_metrics()
    except MetricsError as error:
        raise MetricsError(
            f"cell {result_path.name} ran but the /metrics read after it failed, so "
            f"it has no window and no labels; the next run re-measures it: {error}"
        ) from error
    validate_cell(result_path)
    window = get_prefix_cache_window(
        before,
        after,
        model=model,
        sources=(
            f"/metrics before {result_path.name}",
            f"/metrics after {result_path.name}",
        ),
    )
    labels_uri = write_labels(build_kv_ab_labels(arm=arm, run=run, window=window))
    _LOGGER.info(
        "cell %s prefix cache: hit rate %.3f, combined %.3f",
        labels_uri,
        window.hit_rate,
        window.combined_hit_rate,
    )
    report_cell(publish, result_path=result_path, result_uri=result_uri)
    return labels_uri


def _kv_ab_cell(
    *,
    run_id: str,
    digest: str,
    point_slug: str,
    cell_name: str,
    execute_func: CellExecution,
    read_metrics: MetricsReader,
    result_path: Path,
    arm: Arm,
    run: int,
    model: str,
    result_uri: str,
    write_labels: LabelsWriter,
) -> str:
    """Measure one rung and return its labels' pointer — the function Prefect wraps.

    ``run_id``, ``digest``, ``point_slug``, and ``cell_name`` are unread: they reach the
    cache key only as call parameters (see
    :func:`slipstream_bench.orchestration.tasks.cell._bench_cell`).

    :return: the labels' S3 pointer.
    """
    return run_kv_ab_cell(
        execute_func,
        read_metrics=read_metrics,
        result_path=result_path,
        arm=arm,
        run=run,
        model=model,
        result_uri=result_uri,
        write_labels=write_labels,
        publish=create_markdown_artifact,
    )
