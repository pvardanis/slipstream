"""One @task per cell: return the pointer on a valid result, raise so failures aren't cached.

ADR-0012: the bench task wraps a single cell, returns the cell's S3 pointer, and
raises on an invalid result so a failure is never cached. Keyed
``digest:point-slug:cell-name``, a re-run skips a valid cell (Prefect enters
``Cached`` and does not re-execute) and re-attempts a degenerate one (the raise left
nothing to cache). The pure :func:`run_cell` core is tested without Prefect; the
caching and re-attempt behaviour is exercised under Prefect's test harness.
"""

import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from prefect import flow
from prefect.testing.utilities import prefect_test_harness

from slipstream_bench.orchestration.cell_task import build_cell_task, run_cell
from slipstream_bench.orchestration.validity import InvalidCellError

_VALID = {
    "max_concurrency": 64,
    "prefix_share": 50,
    "num_prompts": 100,
    "completed": 99,
    "request_goodput": 90.0,
    "request_throughput": 95.0,
    "errors": [""] * 99 + ["Timeout"],
}
_DEGENERATE = {
    **_VALID,
    "completed": 0,
    "request_goodput": 0.0,
    "request_throughput": 0.0,
    "errors": ["Connection refused"] * 100,
}


@pytest.fixture(scope="module", autouse=True)
def _harness() -> Iterator[None]:
    with prefect_test_harness():
        yield


def _writer(
    path: Path, record: Mapping[str, object], calls: list[int]
) -> Callable[[], None]:
    def execute() -> None:
        calls.append(1)
        path.write_text(json.dumps(record), encoding="utf-8")

    return execute


def _isolated_task(tmp_path: Path):
    """Build a task whose result and cache storage live under this test's tmp dir.

    Left at Prefect's defaults, cache records land in the shared ``~/.prefect/storage``
    and leak between runs, so a prior run's key would falsely read as a hit. Rooting
    both under ``tmp_path`` keeps each test's cache to itself.
    """
    from uuid import uuid4

    from prefect.filesystems import LocalFileSystem

    results = LocalFileSystem(basepath=str(tmp_path / "results"))
    results.save(f"results-{uuid4().hex}", overwrite=True)
    return build_cell_task(
        result_storage=results, key_storage=str(tmp_path / "cache-keys")
    )


def test_run_cell_returns_the_pointer_on_a_valid_result(tmp_path: Path) -> None:
    path = tmp_path / "cell.json"
    calls: list[int] = []

    uri = run_cell(
        _writer(path, _VALID, calls), result_path=path, result_uri="s3://b/cell.json"
    )

    assert uri == "s3://b/cell.json"
    assert calls == [1]


def test_run_cell_propagates_an_execution_failure(tmp_path: Path) -> None:
    def execute() -> None:
        raise RuntimeError("docker run exited 1")

    with pytest.raises(RuntimeError, match="docker run"):
        run_cell(execute, result_path=tmp_path / "cell.json", result_uri="s3://b/c")


def test_run_cell_raises_on_a_degenerate_result(tmp_path: Path) -> None:
    path = tmp_path / "cell.json"

    with pytest.raises(InvalidCellError):
        run_cell(
            _writer(path, _DEGENERATE, []), result_path=path, result_uri="s3://b/c"
        )


def test_a_valid_cell_is_cached_and_not_re_executed(tmp_path: Path) -> None:
    path = tmp_path / "cell.json"
    calls: list[int] = []
    task = _isolated_task(tmp_path)
    execute = _writer(path, _VALID, calls)

    @flow
    def drive() -> str:
        return task(
            digest="d1",
            point_slug="mns64_kvfp8_pcon",
            cell_name="pshare50_mc64",
            execute_func=execute,
            result_path=path,
            result_uri="s3://b/cell.json",
        )

    first = drive()
    second = drive()

    assert first == second == "s3://b/cell.json"
    assert calls == [1]  # the second run hit the cache and did not re-execute


def test_a_degenerate_cell_is_re_attempted(tmp_path: Path) -> None:
    path = tmp_path / "cell.json"
    calls: list[int] = []
    task = _isolated_task(tmp_path)
    execute = _writer(path, _DEGENERATE, calls)

    @flow
    def drive() -> str:
        return task(
            digest="d2",
            point_slug="mns64_kvfp8_pcon",
            cell_name="pshare90_mc256",
            execute_func=execute,
            result_path=path,
            result_uri="s3://b/cell.json",
        )

    for _ in range(2):
        with pytest.raises(InvalidCellError):
            drive()

    assert calls == [1, 1]  # nothing was cached, so each run re-attempted the cell


def test_cell_task_for_a_bucket_wires_s3_result_storage() -> None:
    from prefect_aws import S3Bucket

    from slipstream_bench.orchestration.cell_task import cell_task

    task = cell_task("slipstream-bench-results")

    assert isinstance(task.result_storage, S3Bucket)
    assert task.result_storage.bucket_folder == "prefect/results"
