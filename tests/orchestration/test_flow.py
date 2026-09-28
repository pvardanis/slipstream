"""The point sweep flow: proxy once, one resumable cell task per grid cell.

ADR-0012 §Amendment: the flow drives one engine point's Tier-2 cells. It starts the
loopback proxy once, enumerates the point's cells, and runs each as a cell task keyed
``digest:point-slug:cell-name`` so a re-run skips the cells that already hold a valid
measurement. This exercises that against faked SSM/S3 clients and a tmp-dir-backed
cell task: a first run executes every cell, a second run — same digest — hits the
cache and re-executes none, while the proxy still comes up once per invocation.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from prefect.filesystems import LocalFileSystem
from prefect.testing.utilities import prefect_test_harness

from slipstream_bench.orchestration.cell_task import build_cell_task
from slipstream_bench.orchestration.flow import SweepContext, run_point_sweep
from slipstream_bench.orchestration.validity import InvalidCellError

_PROXY = "/usr/local/bin/bench-proxy-up.sh"
_CELL = "/usr/local/bin/bench-sweep.sh"

_VALID_RESULT = {
    "prefix_share": 10,
    "max_concurrency": 64,
    "num_prompts": 100,
    "completed": 99,
    "request_goodput": 90.0,
    "request_throughput": 95.0,
    "errors": [""] * 99 + ["Timeout"],
}

# 50 of 100 requests failed -> error rate 0.5, past the gate's 0.05 ceiling: parses,
# but an unhealthy-server result the validity gate rejects.
_UNHEALTHY_RESULT = {**_VALID_RESULT, "completed": 50}

# A closed-loop grid for the mns64_kvfp8_pcon point the context names: its 'on' arm
# sweeps shares [10, 50] across the [64, 128] ladder at the pinned burstiness. The gate
# reuses the aggregate-sweep parser, which requires each cell's max_concurrency cap —
# the knob sweep's Tier-2 ladder shape.
_CLOSED_LOOP_GRID = """
tier1:
  max_num_seqs: [64]
  kv_cache_dtype: [fp8]
  prefix_caching:
    "on":
      flag: --enable-prefix-caching
      prefix_share: [10, 50]
    "off":
      flag: --no-enable-prefix-caching
      prefix_share: [0]
tier2:
  max_concurrency: [64, 128]
  burstiness: 1.0
load:
  total_len: 1000
  num_prompts: 500
  num_prefixes: 5
  output_len: 128
  align_blocks: 0
  request_rate: 8
  seed: 0
  goodput: ["ttft:1000", "tpot:50"]
"""


@pytest.fixture(scope="module", autouse=True)
def _harness() -> Iterator[None]:
    with prefect_test_harness():
        yield


class _FakeSsm:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def send_command(self, **kwargs: object) -> dict[str, object]:
        params = kwargs["Parameters"]
        assert isinstance(params, dict)
        self.commands.append(params["commands"][0])
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        return {"Status": "Success", "StandardErrorContent": ""}

    def proxy_ups(self) -> int:
        return sum(1 for c in self.commands if _PROXY in c)

    def cell_runs(self) -> int:
        return sum(1 for c in self.commands if _CELL in c)


class _FakeS3:
    def download_file(self, _bucket: str, _key: str, dest: str) -> None:
        Path(dest).write_text(json.dumps(_VALID_RESULT), encoding="utf-8")


class _UnhealthyOnceS3:
    """Serve one cell an unhealthy result the first time it is fetched, valid after."""

    def __init__(self, unhealthy_basename: str) -> None:
        self._unhealthy = unhealthy_basename
        self._served: set[str] = set()

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        if self._unhealthy in key and key not in self._served:
            self._served.add(key)
            Path(dest).write_text(json.dumps(_UNHEALTHY_RESULT), encoding="utf-8")
        else:
            Path(dest).write_text(json.dumps(_VALID_RESULT), encoding="utf-8")


def _isolated_task(tmp_path: Path):
    results = LocalFileSystem(basepath=str(tmp_path / "results"))
    results.save(f"results-{uuid4().hex}", overwrite=True)
    return build_cell_task(
        result_storage=results, key_storage=str(tmp_path / "cache-keys")
    )


def _context() -> SweepContext:
    return SweepContext(
        run_id="run1/mns64_kvfp8_pcon",
        point_slug="mns64_kvfp8_pcon",
        digest="digest-abc",
        instance_id="i-1",
        image_ref="repo:tag",
        bucket="bench-bucket",
        model="Qwen/Qwen2.5-0.5B-Instruct",
    )


def _grid(tmp_path: Path) -> Path:
    path = tmp_path / "sweep-grid.yaml"
    path.write_text(_CLOSED_LOOP_GRID, encoding="utf-8")
    return path


def _drive(tmp_path: Path, ssm: _FakeSsm, task, s3=None) -> list[str]:
    return run_point_sweep(
        grid_path=_grid(tmp_path),
        results_dir=tmp_path / "results-local",
        context=_context(),
        ssm_client=ssm,
        s3_client=s3 or _FakeS3(),
        task=task,
        poll_interval_s=0.0,
        sleep=lambda _s: None,
    )


def test_first_run_executes_every_cell(tmp_path: Path) -> None:
    ssm = _FakeSsm()
    task = _isolated_task(tmp_path)

    pointers = _drive(tmp_path, ssm, task)

    # 2 prefix_shares x 1 burstiness x 2 concurrency rungs = 4 cells.
    assert len(pointers) == 4
    assert all(p.startswith("s3://bench-bucket/sweeps/run1/") for p in pointers)
    assert ssm.proxy_ups() == 1
    assert ssm.cell_runs() == 4


def test_second_run_resumes_and_re_executes_no_cell(tmp_path: Path) -> None:
    task = _isolated_task(tmp_path)

    first_ssm = _FakeSsm()
    first = _drive(tmp_path, first_ssm, task)

    second_ssm = _FakeSsm()
    second = _drive(tmp_path, second_ssm, task)

    assert first == second
    assert second_ssm.proxy_ups() == 1  # the proxy still comes up once per flow
    assert second_ssm.cell_runs() == 0  # every cell hit the cache


def test_a_failed_cell_re_runs_while_valid_cells_stay_cached(tmp_path: Path) -> None:
    task = _isolated_task(tmp_path)
    # The ladder is innermost, shares outermost, so this is the last enumerated cell:
    # the three before it cache before it fails the gate and raises out of the flow.
    unhealthy = "pshare50_burst1.0_mc128.json"

    first_ssm = _FakeSsm()
    with pytest.raises(InvalidCellError):
        _drive(tmp_path, first_ssm, task, s3=_UnhealthyOnceS3(unhealthy))

    second_ssm = _FakeSsm()
    _drive(tmp_path, second_ssm, task, s3=_FakeS3())

    assert second_ssm.cell_runs() == 1  # only the once-failed cell re-runs
    assert second_ssm.proxy_ups() == 1
