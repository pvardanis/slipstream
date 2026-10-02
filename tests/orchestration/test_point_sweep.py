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
from prefect import Task
from prefect.filesystems import LocalFileSystem
from prefect.testing.utilities import prefect_test_harness

from slipstream_bench.orchestration.flows.point_sweep import (
    SweepContext,
    run_point_sweep,
)
from slipstream_bench.orchestration.tasks.cell import build_cell_task
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


class _DestRecordingS3:
    """Record each download destination, serving every cell a valid result."""

    def __init__(self) -> None:
        self.dests: list[str] = []

    def download_file(self, _bucket: str, _key: str, dest: str) -> None:
        self.dests.append(dest)
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


def test_cell_results_download_into_an_ephemeral_dir_cleaned_after(
    tmp_path: Path,
) -> None:
    """Each cell's result JSON downloads into a scratch dir the flow owns and removes.

    The worker runs under a read-only root filesystem, so the download target cannot be
    a fixed in-image path; it is an ephemeral directory created and torn down inside the
    flow. The validity gate reads each result while the flow runs, then nothing it
    downloaded survives the flow — no fixed results path is written to.
    """
    recorder = _DestRecordingS3()

    pointers = run_point_sweep(
        grid_path=_grid(tmp_path),
        context=_context(),
        ssm_client=_FakeSsm(),
        s3_client=recorder,
        task=_isolated_task(tmp_path),
        poll_interval_s=0.0,
        sleep=lambda _s: None,
    )

    assert len(pointers) == 4
    assert recorder.dests  # every cell was downloaded somewhere
    assert not any(Path(dest).exists() for dest in recorder.dests)
    assert not any(Path(dest).parent.exists() for dest in recorder.dests)


def test_no_cell_command_carries_a_tokenizer_env(tmp_path: Path) -> None:
    """The cell's tokenizer is the image's baked snapshot, not an SSM-passed env.

    The image names where its tokenizer lives (the baked path, read by the cell's
    ``--tokenizer`` env var), so the orchestrator passes no tokenizer or revision env.
    """
    ssm = _FakeSsm()

    _drive(tmp_path, ssm, _isolated_task(tmp_path))

    cell_commands = [c for c in ssm.commands if _CELL in c]
    assert cell_commands
    assert all("REVISION" not in command for command in cell_commands)
    assert all("TOKENIZER" not in command for command in cell_commands)


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


def test_run_group_is_the_run_id_prefix_shared_across_a_sweep() -> None:
    # run_id is <run>/<point-slug>; the leading <run> is the knob sweep's single run,
    # the tag every one of its points' flow runs groups under.
    assert _context().run_group == "run1"


class _FlowRunRecordingTask:
    """Stand in for the cell task, recording the flow run's name and tags per cell.

    Reads :mod:`prefect.runtime` from inside the running flow, so each cell records the
    parent point-sweep flow run's name and tags — the grouping :func:`run_point_sweep`
    stamps — and returns the cell's pointer without touching SSM or S3.
    """

    def __init__(self) -> None:
        self.names: list[str | None] = []
        self.tag_sets: list[set[str]] = []

    def with_options(self, *, tags: list[str]) -> "_FlowRunRecordingTask":
        return self

    def __call__(self, **kwargs: object) -> object:
        from prefect.runtime import flow_run

        self.names.append(flow_run.name)
        self.tag_sets.append(set(flow_run.tags))
        return kwargs["result_uri"]


def test_every_point_flow_run_is_named_and_grouped(tmp_path: Path) -> None:
    recorder = _FlowRunRecordingTask()

    run_point_sweep(
        grid_path=_grid(tmp_path),
        context=_context(),
        ssm_client=_FakeSsm(),
        s3_client=_FakeS3(),
        task=recorder,  # ty: ignore[invalid-argument-type]  # duck-typed task double
        poll_interval_s=0.0,
        sleep=lambda _s: None,
    )

    # Every cell runs under the one point-sweep flow run: named by the point slug and
    # tagged with the shared run, so the UI groups the whole knob sweep by run=run1.
    assert recorder.names == ["mns64_kvfp8_pcon"] * 4
    assert all("run=run1" in tags for tags in recorder.tag_sets)


class _TagRecordingTask:
    """Wrap the real cell task, recording the tags each cell run is labelled with."""

    def __init__(self, inner: Task[..., str]) -> None:
        self._inner = inner
        self.tag_sets: list[list[str]] = []

    def with_options(self, *, tags: list[str]) -> Task[..., str]:
        self.tag_sets.append(list(tags))
        return self._inner.with_options(tags=tags)


def test_each_cell_run_is_tagged_with_its_tier_knobs(tmp_path: Path) -> None:
    recorder = _TagRecordingTask(_isolated_task(tmp_path))

    run_point_sweep(
        grid_path=_grid(tmp_path),
        context=_context(),
        ssm_client=_FakeSsm(),
        s3_client=_FakeS3(),
        task=recorder,  # ty: ignore[invalid-argument-type]  # duck-typed task double
        poll_interval_s=0.0,
        sleep=lambda _s: None,
    )

    # 2 shares x 2 concurrency rungs = 4 cells, each carrying the point's shared tier1
    # tags and its own tier2 tags.
    assert len(recorder.tag_sets) == 4
    for tags in recorder.tag_sets:
        assert {"mns=64", "kv=fp8", "pc=on", "burst=1.0"} <= set(tags)
    caps = {tag for tags in recorder.tag_sets for tag in tags if tag.startswith("mc=")}
    shares = {t for tags in recorder.tag_sets for t in tags if t.startswith("pshare=")}
    assert caps == {"mc=64", "mc=128"}
    assert shares == {"pshare=10", "pshare=50"}
