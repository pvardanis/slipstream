"""Tests for the kv-ab cell task: a measured rung, its windowed record, and its cache.

A rung reads vLLM's /metrics before and after its cell, gates the result, and writes the
result labelled with the arm, run, and the prefix-cache counters' advance over the cell.
The Prefect-free :func:`run_kv_ab_cell` is tested against fakes; the task's caching and
refresh are exercised under Prefect's test harness.
"""

import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prefect import Task, flow
from prefect.filesystems import LocalFileSystem
from prefect.testing.utilities import prefect_test_harness

from slipstream_bench.contract.prefix_cache_window import PrefixCacheError
from slipstream_bench.orchestration.tasks.kv_ab_cell import (
    build_kv_ab_cell_task,
    run_kv_ab_cell,
)
from slipstream_bench.orchestration.validity import InvalidCellError

_MODEL = "Qwen/Qwen3-8B-AWQ"
_RECORD_URI = "s3://bench/kv-ab/ab1/on-r1/pshare90_burst1.0_mc4.json"

_VALID = {
    "max_concurrency": 4,
    "prefix_share": 90,
    "num_prompts": 128,
    "completed": 128,
    "request_goodput": 2.0,
    "request_throughput": 2.0,
    "p95_ttft_ms": 850.0,
    "p95_tpot_ms": 42.0,
    "output_throughput": 256.0,
    "errors": [""] * 128,
}
_DEGENERATE = {**_VALID, "completed": 0, "errors": ["Connection refused"] * 128}


def _exposition(
    queries: int, hits: int, external_queries: int, external_hits: int
) -> str:
    counters = {
        "vllm:prefix_cache_queries": queries,
        "vllm:prefix_cache_hits": hits,
        "vllm:external_prefix_cache_queries": external_queries,
        "vllm:external_prefix_cache_hits": external_hits,
    }
    lines = []
    for metric, value in counters.items():
        lines.append(f"# TYPE {metric} counter")
        lines.append(f'{metric}_total{{model_name="{_MODEL}"}} {value}')
    return "\n".join(lines) + "\n"


# 10,000 queries and 9,000 GPU hits before the cell, from the warmup and prior rungs; the
# cell itself queries 1,000 tokens, 500 served by the GPU and 300 by the CPU tier.
_BEFORE = _exposition(10_000, 9_000, 1_000, 900)
_AFTER = _exposition(11_000, 9_500, 1_500, 1_200)


class _Rung:
    """One rung's fakes: the /metrics reads, the cell run, and the record write, in order."""

    def __init__(
        self,
        result_path: Path,
        result: Mapping[str, object] = _VALID,
        snapshots: tuple[str, str] = (_BEFORE, _AFTER),
    ) -> None:
        self._result_path = result_path
        self._result = result
        self._snapshots = iter(snapshots)
        self.calls: list[str] = []
        self.records: list[Mapping[str, Any]] = []

    def read_metrics(self) -> str:
        self.calls.append("metrics")
        return next(self._snapshots)

    def execute(self) -> None:
        self.calls.append("cell")
        self._result_path.write_text(json.dumps(self._result), encoding="utf-8")

    def write_record(self, record: Mapping[str, Any]) -> str:
        self.calls.append("record")
        self.records.append(record)
        return _RECORD_URI

    def run(self) -> str:
        return run_kv_ab_cell(
            self.execute,
            read_metrics=self.read_metrics,
            result_path=self._result_path,
            arm="on",
            run=1,
            model=_MODEL,
            write_record=self.write_record,
            publish=_discard,
        )


def _discard(**_: object) -> None:
    """A publish stand-in for the artifact the record's rung draws."""


def test_the_hit_rate_is_the_cells_window_not_the_lifetime_ratio(
    tmp_path: Path,
) -> None:
    rung = _Rung(tmp_path / "cell.json")

    rung.run()

    (record,) = rung.records
    # 500/1,000 over the cell, where the after snapshot's lifetime ratio is 9,500/11,000.
    assert record["kv_ab"]["hit_rate"] == 0.5
    assert record["kv_ab"]["combined_hit_rate"] == 0.8
    assert record["completed"] == 128


def test_the_metrics_reads_bracket_the_cell_and_the_record_follows(
    tmp_path: Path,
) -> None:
    rung = _Rung(tmp_path / "cell.json")

    uri = rung.run()

    assert rung.calls == ["metrics", "cell", "metrics", "record"]
    assert uri == _RECORD_URI


def test_an_invalid_cell_writes_no_record(tmp_path: Path) -> None:
    rung = _Rung(tmp_path / "cell.json", result=_DEGENERATE)

    with pytest.raises(InvalidCellError):
        rung.run()

    assert rung.records == []


def test_a_cell_whose_counters_went_backwards_writes_no_record(
    tmp_path: Path,
) -> None:
    # The engine restarted mid-cell, so the counters reset and the delta is meaningless.
    rung = _Rung(tmp_path / "cell.json", snapshots=(_AFTER, _BEFORE))

    with pytest.raises(PrefixCacheError, match="backwards"):
        rung.run()

    assert rung.records == []


@pytest.fixture(scope="module")
def _harness() -> Iterator[None]:
    with prefect_test_harness():
        yield


def _isolated_task(tmp_path: Path) -> Task[..., str]:
    results = LocalFileSystem(basepath=str(tmp_path / "results"))
    results.save(f"results-{uuid4().hex}", overwrite=True)
    keys = LocalFileSystem(basepath=str(tmp_path / "cache-keys"))
    keys.save(f"cache-keys-{uuid4().hex}", overwrite=True)
    return build_kv_ab_cell_task(result_storage=results, key_storage=keys)


def _call(task: Task[..., str], rung: _Rung, result_path: Path) -> Callable[[], str]:
    @flow
    def _one_rung() -> str:
        return task(
            run_id="kv-ab/ab1/on-r1",
            digest="d" * 64,
            point_slug="on-r1",
            cell_name="pshare90_burst1.0_mc4.json",
            execute_func=rung.execute,
            read_metrics=rung.read_metrics,
            result_path=result_path,
            arm="on",
            run=1,
            model=_MODEL,
            write_record=rung.write_record,
        )

    return _one_rung


@pytest.mark.usefixtures("_harness")
def test_a_measured_rung_is_cached_and_not_re_measured(tmp_path: Path) -> None:
    task = _isolated_task(tmp_path)
    first = _Rung(tmp_path / "a.json")
    second = _Rung(tmp_path / "b.json")

    assert _call(task, first, tmp_path / "a.json")() == _RECORD_URI
    assert _call(task, second, tmp_path / "b.json")() == _RECORD_URI

    assert second.calls == []


@pytest.mark.usefixtures("_harness")
def test_a_refreshing_task_re_measures_a_cached_rung(tmp_path: Path) -> None:
    task = _isolated_task(tmp_path)
    _call(task, _Rung(tmp_path / "a.json"), tmp_path / "a.json")()
    again = _Rung(tmp_path / "b.json")

    _call(task.with_options(refresh_cache=True), again, tmp_path / "b.json")()

    assert again.calls == ["metrics", "cell", "metrics", "record"]
