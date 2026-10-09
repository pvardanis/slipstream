"""One @task per cell: return the pointer on a valid result, raise so failures aren't cached.

ADR-0012: the bench task wraps a single cell, returns the cell's S3 pointer, and
raises on an invalid result so a failure is never cached. Keyed
``digest:point-slug:cell-name``, a re-run skips a valid cell (Prefect enters
``Cached`` and does not re-execute) and re-attempts a degenerate one (the raise left
nothing to cache). The pure :func:`run_cell` core is tested without Prefect; the
caching and re-attempt behaviour is exercised under Prefect's test harness.
"""

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from prefect import flow
from prefect.testing.utilities import prefect_test_harness

from slipstream_bench.orchestration.tasks.cell import build_cell_task, run_cell
from slipstream_bench.orchestration.validity import InvalidCellError

_VALID = {
    "max_concurrency": 64,
    "prefix_share": 50,
    "num_prompts": 100,
    "completed": 99,
    "request_goodput": 90.0,
    "request_throughput": 95.0,
    "p95_ttft_ms": 850.0,
    "p95_tpot_ms": 42.0,
    "output_throughput": 1234.5,
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


def _discard(**_: object) -> None:
    """A publish stand-in for the runs that never reach the artifact."""


def _collector() -> tuple[list[dict[str, object]], Callable[..., None]]:
    """A publish stand-in that records each artifact call's kwargs for assertion."""
    calls: list[dict[str, object]] = []

    def publish(**kwargs: object) -> None:
        calls.append(kwargs)

    return calls, publish


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
        _writer(path, _VALID, calls),
        result_path=path,
        result_uri="s3://b/cell.json",
        publish=_discard,
    )

    assert uri == "s3://b/cell.json"
    assert calls == [1]


def test_run_cell_logs_a_start_and_a_done_gate_milestone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The cell's two progress milestones an operator reads off its task run page: a start
    # line as the ~minutes-long benchmark begins, and a done line carrying its SLO-gate
    # result — the goodput fraction the run cleared the validity gate with and the output
    # token rate it sustained, the four headline numbers that match its artifact table
    # (ADR-0020). A cache hit skips this body entirely, so a resumed cell draws neither line.
    path = tmp_path / "cell.json"

    with caplog.at_level(
        logging.INFO, logger="slipstream_bench.orchestration.tasks.cell"
    ):
        run_cell(
            _writer(path, _VALID, []),
            result_path=path,
            result_uri="s3://b/cell.json",
            publish=_discard,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("s3://b/cell.json" in m and "starting" in m for m in messages)
    assert any(
        "s3://b/cell.json" in m and "done" in m and "0.947" in m and "1234.5" in m
        for m in messages
    )


def test_run_cell_publishes_the_cell_metrics_artifact(tmp_path: Path) -> None:
    # A valid cell publishes an unkeyed markdown artifact onto its own task run: the S3
    # pointer and the four headline metrics (goodput, the two p95 SLO gates, the output
    # token rate), so the sub-task reads its result without opening S3. Unkeyed so it
    # lands in this task run's own Artifacts tab rather than a cross-run timeline.
    path = tmp_path / "cell.json"
    calls, publish = _collector()

    run_cell(
        _writer(path, _VALID, []),
        result_path=path,
        result_uri="s3://b/cell.json",
        publish=publish,
    )

    assert len(calls) == 1
    assert calls[0]["key"] is None
    markdown = calls[0]["markdown"]
    assert isinstance(markdown, str)
    assert "s3://b/cell.json" in markdown
    assert "0.947" in markdown  # goodput fraction: 90.0 / 95.0
    assert "850" in markdown  # p95 ttft ms
    assert "42" in markdown  # p95 tpot ms
    assert "1234.5" in markdown  # output throughput


def test_run_cell_survives_a_publish_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The artifact is a UI convenience; the measurement is already durable in S3. A
    # publish failure must not raise out of the task body — that would leave the cell
    # uncached and force a re-run of the expensive GPU benchmark (ADR-0012). The pointer
    # is returned and the failure is logged with the cell's pointer for context.
    path = tmp_path / "cell.json"

    def boom(**_: object) -> None:
        raise RuntimeError("prefect API unreachable")

    with caplog.at_level(logging.WARNING):
        uri = run_cell(
            _writer(path, _VALID, []),
            result_path=path,
            result_uri="s3://b/cell.json",
            publish=boom,
        )

    assert uri == "s3://b/cell.json"
    assert "s3://b/cell.json" in caplog.text
    assert "prefect API unreachable" in caplog.text


def test_run_cell_propagates_a_markdown_render_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Building the artifact markdown is pure formatting off the gated cell, not the publish
    # I/O: a defect there is a local code bug, not a transient Prefect-API failure, so it
    # must raise rather than be swallowed and mislabeled "failed to publish". Only the
    # publish call itself is best-effort.
    from slipstream_bench.orchestration.tasks import cell as cell_module

    path = tmp_path / "cell.json"

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("markdown render bug")

    monkeypatch.setattr(cell_module, "_cell_artifact_markdown", boom)

    with pytest.raises(RuntimeError, match="markdown render bug"):
        run_cell(
            _writer(path, _VALID, []),
            result_path=path,
            result_uri="s3://b/cell.json",
            publish=_discard,
        )


def test_run_cell_propagates_an_execution_failure(tmp_path: Path) -> None:
    def execute() -> None:
        raise RuntimeError("docker run exited 1")

    with pytest.raises(RuntimeError, match="docker run"):
        run_cell(
            execute,
            result_path=tmp_path / "cell.json",
            result_uri="s3://b/c",
            publish=_discard,
        )


def test_run_cell_raises_on_a_degenerate_result(tmp_path: Path) -> None:
    path = tmp_path / "cell.json"

    with pytest.raises(InvalidCellError):
        run_cell(
            _writer(path, _DEGENERATE, []),
            result_path=path,
            result_uri="s3://b/c",
            publish=_discard,
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

    from slipstream_bench.orchestration.tasks.cell import cell_task

    task = cell_task("slipstream-bench-results")

    assert isinstance(task.result_storage, S3Bucket)
    assert task.result_storage.bucket_folder == "prefect/results"


def test_the_task_run_name_reads_the_point_slug_and_cell_name(tmp_path: Path) -> None:
    task = _isolated_task(tmp_path)

    # Prefect fills the template from the task's call parameters, so a run reads its
    # grid coordinate — tier1 point slug, then tier2 cell name — in the run list.
    assert task.task_run_name == "{point_slug}:{cell_name}"


def test_the_task_run_name_renders_from_the_call_parameters(tmp_path: Path) -> None:
    from prefect.runtime import task_run

    task = _isolated_task(tmp_path)
    path = tmp_path / "cell.json"
    rendered: list[str | None] = []

    def execute() -> None:
        # Read the name Prefect rendered for this run, so a drift between the
        # template's placeholders and the task's call parameters fails here.
        rendered.append(task_run.get_name())
        path.write_text(json.dumps(_VALID), encoding="utf-8")

    @flow
    def drive() -> str:
        return task(
            digest="d1",
            point_slug="mns64_kvfp8_pcon",
            cell_name="pshare50_burst1.0_mc64",
            execute_func=execute,
            result_path=path,
            result_uri="s3://b/cell.json",
        )

    drive()

    assert rendered == ["mns64_kvfp8_pcon:pshare50_burst1.0_mc64"]
