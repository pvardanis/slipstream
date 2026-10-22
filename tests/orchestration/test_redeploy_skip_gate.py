"""The redeploy-skip gate and the cell task agree on which cells are complete.

ADR-0015's gate decides whether a knob sweep redeploys an engine point; the cell task's
cache decides whether a cell re-runs. Both read Prefect's cache records, so they cannot
disagree. This drives :func:`drive_knob_sweep` with the production point sweep and gate
from :func:`build_knob_sweep_collaborators`, over real Prefect tasks whose result and
cache-key storage live on a tmp local filesystem, with SSM and S3 faked: each test asks
whether the sweep redeploys the point and which of its cells run or come from cache.
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
import yaml
from prefect import Task
from prefect.filesystems import LocalFileSystem
from prefect.testing.utilities import prefect_test_harness

from slipstream_bench.contract import EnginePoint, SweepGrid, list_engine_points
from slipstream_bench.orchestration.cell_run import CellResultError
from slipstream_bench.orchestration.digest import ServingConfig
from slipstream_bench.orchestration.flows.knob_sweep import (
    KnobSweepInputs,
    SweepOutcome,
    build_knob_sweep_collaborators,
    drive_knob_sweep,
)
from slipstream_bench.orchestration.tasks.cell import build_cell_task
from slipstream_bench.orchestration.validity import InvalidCellError

_CELL = "/usr/local/bin/bench-sweep.sh"

# One engine point (prefix caching on, mns64, fp8) with two cells: shares 10 and 50 at
# the single concurrency rung, enumerated in that order.
_GRID = """
tier1:
  max_num_seqs: [64]
  kv_cache_dtype: [fp8]
  prefix_caching:
    "on":
      flag: --enable-prefix-caching
      prefix_share: [10, 50]
tier2:
  max_concurrency: [64]
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

_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-gpu
  namespace: slipstream
spec:
  template:
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:v0.6.0
"""

# The logger the gate names a pending point's first uncached cell on.
_GATE_LOGGER = "slipstream_bench.orchestration.flows.knob_sweep"

# The point's last enumerated cell, the one the failure tests break.
_LAST_CELL = "pshare50_burst1.0_mc64.json"

_VALID_RESULT = {
    "prefix_share": 10,
    "max_concurrency": 64,
    "num_prompts": 100,
    "completed": 99,
    "request_goodput": 90.0,
    "request_throughput": 95.0,
    "p95_ttft_ms": 850.0,
    "p95_tpot_ms": 42.0,
    "output_throughput": 1234.5,
    "errors": [""] * 99 + ["Timeout"],
}

# 50 of 100 requests failed: past the validity gate's error-rate ceiling, so degenerate.
_DEGENERATE_RESULT = {**_VALID_RESULT, "completed": 50}


@pytest.fixture(scope="module", autouse=True)
def _harness() -> Iterator[None]:
    with prefect_test_harness():
        yield


class _FakeSsm:
    """Accept every command, counting the cell runs among them."""

    def __init__(self) -> None:
        self.cell_runs = 0

    def send_command(self, **kwargs: Any) -> dict[str, object]:
        if _CELL in kwargs["Parameters"]["commands"][0]:
            self.cell_runs += 1
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        return {"Status": "Success", "StandardErrorContent": ""}


# How the broken cell misbehaves: its fetch fails outright, or it serves a result the
# validity gate rejects.
_BreakMode = Literal["raise", "degenerate"]


class _CellS3:
    """Serve every cell a valid result, except one that misbehaves on its first fetch.

    ``broken`` names the cell basename to break (none when empty); ``mode`` is how it
    breaks.
    """

    def __init__(self, broken: str = "", mode: _BreakMode = "raise") -> None:
        self._broken = broken
        self._mode = mode
        self._served: set[str] = set()

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        result = _VALID_RESULT
        if self._broken and key.endswith(self._broken) and key not in self._served:
            self._served.add(key)
            # Only the first fetch misbehaves, so a resumed sweep re-runs the cell cleanly.
            if self._mode == "raise":
                raise RuntimeError(f"cell object {key} could not be fetched")
            result = _DEGENERATE_RESULT
        Path(dest).write_text(json.dumps(result), encoding="utf-8")


class _Sweep:
    """One knob sweep's production gate and point sweep, over a shared cell task."""

    def __init__(
        self,
        tmp_path: Path,
        task: Task[..., str],
        *,
        vllm_image: str = "vllm/vllm-openai:v0.6.0",
        grid: str = _GRID,
        run: str = "run1",
    ) -> None:
        grid_path = tmp_path / "sweep-grid.yaml"
        grid_path.write_text(grid, encoding="utf-8")
        manifest_path = tmp_path / "vllm-gpu.yaml"
        manifest_path.write_text(_MANIFEST, encoding="utf-8")
        self._inputs = KnobSweepInputs(
            grid=SweepGrid.model_validate(yaml.safe_load(grid)),
            grid_path=grid_path,
            manifest_path=manifest_path,
            run=run,
            serving=ServingConfig(model_yaml=b"model: {}\n", vllm_image_ref=vllm_image),
            instance_id="i-1",
            image_ref="repo:tag",
            bucket="bench-bucket",
            model="Qwen/Qwen2.5-0.5B-Instruct",
        )
        self._task = task
        self.point, *self.other_points = list_engine_points(self._inputs.grid)
        self.ssm = _FakeSsm()
        self.deployed: list[EnginePoint] = []

    def _collaborators(self, s3: _CellS3) -> tuple[Any, Any, Any, Any]:
        return build_knob_sweep_collaborators(
            self._inputs,
            kubectl=lambda *_a, **_k: "",
            ssm_client=self.ssm,
            s3_client=s3,
            task=self._task,
        )

    def is_pending(self, point: EnginePoint | None = None) -> bool:
        """Ask the production gate whether a point (the first by default) has cells to run."""
        *_rest, has_pending_cells = self._collaborators(_CellS3())
        return has_pending_cells(point or self.point)

    def run(
        self, s3: _CellS3 | None = None, *, every_point: bool = False
    ) -> SweepOutcome:
        """Drive the sweep, recording each redeploy; the ceiling scrape is a no-op.

        Only the first point is swept unless ``every_point`` is set.
        """
        _deploy, _scrape, point_sweep_fn, has_pending_cells = self._collaborators(
            s3 or _CellS3()
        )
        return drive_knob_sweep(
            points=[self.point, *self.other_points] if every_point else [self.point],
            deploy_fn=self.deployed.append,
            scrape_fn=lambda _point: None,
            point_sweep_fn=point_sweep_fn,
            has_pending_cells=has_pending_cells,
        )


def _isolated_task(tmp_path: Path) -> Task[..., str]:
    results = LocalFileSystem(basepath=str(tmp_path / "results"))
    results.save(f"results-{uuid4().hex}", overwrite=True)
    keys = LocalFileSystem(basepath=str(tmp_path / "cache-keys"))
    keys.save(f"cache-keys-{uuid4().hex}", overwrite=True)
    return build_cell_task(result_storage=results, key_storage=keys)


def test_a_fresh_point_is_pending_and_redeploys(tmp_path: Path) -> None:
    sweep = _Sweep(tmp_path, _isolated_task(tmp_path))

    assert sweep.is_pending() is True
    sweep.run()

    assert sweep.deployed == [sweep.point]
    assert sweep.ssm.cell_runs == 2


def test_a_completed_point_is_skipped_and_every_cell_is_cached(
    tmp_path: Path,
) -> None:
    task = _isolated_task(tmp_path)
    _Sweep(tmp_path, task).run()

    resume = _Sweep(tmp_path, task)
    assert resume.is_pending() is False
    outcome = resume.run()

    assert resume.deployed == []  # no redeploy, no ceiling scrape
    assert resume.ssm.cell_runs == 0  # every cell came from cache
    assert outcome.cells_resumed == 2


def test_a_vllm_image_change_redeploys_the_point_and_re_runs_its_cells(
    tmp_path: Path,
) -> None:
    task = _isolated_task(tmp_path)
    _Sweep(tmp_path, task).run()

    changed = _Sweep(tmp_path, task, vllm_image="vllm/vllm-openai:v0.7.0")
    assert changed.is_pending() is True
    changed.run()

    # The gate and the cell task agree: the point redeploys and both cells re-measure
    # against the new engine, none is served from the old digest's cache.
    assert changed.deployed == [changed.point]
    assert changed.ssm.cell_runs == 2


def test_a_failed_cell_leaves_its_point_pending_and_only_it_re_runs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    task = _isolated_task(tmp_path)
    with pytest.raises(CellResultError, match=_LAST_CELL):
        _Sweep(tmp_path, task).run(_CellS3(_LAST_CELL, "raise"))

    resume = _Sweep(tmp_path, task)
    with caplog.at_level(logging.INFO, logger=_GATE_LOGGER):
        assert resume.is_pending() is True
    assert f"cell {_LAST_CELL} has no cache record" in caplog.text
    resume.run()

    assert resume.deployed == [resume.point]
    assert resume.ssm.cell_runs == 1  # only the failed cell re-runs


def test_a_degenerate_cell_is_not_cached_and_its_point_stays_pending(
    tmp_path: Path,
) -> None:
    task = _isolated_task(tmp_path)
    with pytest.raises(InvalidCellError):
        _Sweep(tmp_path, task).run(_CellS3(_LAST_CELL, "degenerate"))

    resume = _Sweep(tmp_path, task)
    assert resume.is_pending() is True
    resume.run()

    assert resume.deployed == [resume.point]
    assert resume.ssm.cell_runs == 1  # the degenerate cell re-runs, the valid one stays


def test_a_completed_point_leaves_another_point_with_the_same_cells_pending(
    tmp_path: Path,
) -> None:
    # Two engine points (mns64, mns128) sharing the same two cell names: only the slug
    # tells their cache records apart.
    grid = _GRID.replace("max_num_seqs: [64]", "max_num_seqs: [64, 128]")
    sweep = _Sweep(tmp_path, _isolated_task(tmp_path), grid=grid)
    sweep.run()  # runs the first point only
    (other,) = sweep.other_points

    assert sweep.is_pending() is False
    assert sweep.is_pending(other) is True


def test_a_refreshing_task_leaves_a_completed_point_pending(tmp_path: Path) -> None:
    task = _isolated_task(tmp_path)
    _Sweep(tmp_path, task).run()

    # A refreshing task overwrites its cache records instead of reading them, so every
    # cell re-runs: the gate must redeploy the point rather than skip it.
    refresh = _Sweep(tmp_path, task.with_options(refresh_cache=True))
    assert refresh.is_pending() is True
    refresh.run()

    assert refresh.deployed == [refresh.point]
    assert refresh.ssm.cell_runs == 2


def test_a_new_run_id_re_measures_every_cell_under_its_own_prefix(
    tmp_path: Path,
) -> None:
    task = _isolated_task(tmp_path)
    _Sweep(tmp_path, task, run="run1").run()

    fresh = _Sweep(tmp_path, task, run="run2")
    assert fresh.is_pending() is True
    outcome = fresh.run()

    # A re-dispatch with a fresh run id shares nothing with the previous run's cache:
    # the point redeploys, every cell re-measures, and no pointer leads into run1.
    assert fresh.deployed == [fresh.point]
    assert fresh.ssm.cell_runs == 2
    assert outcome.cells_resumed == 0
    assert len(outcome.pointers) == 2
    assert all(
        pointer.startswith("s3://bench-bucket/knob-sweep/run2/")
        for pointer in outcome.pointers
    )


def test_the_same_run_id_and_config_re_executes_nothing(tmp_path: Path) -> None:
    task = _isolated_task(tmp_path)
    first = _Sweep(tmp_path, task, run="run1").run()

    resume = _Sweep(tmp_path, task, run="run1")
    assert resume.is_pending() is False
    outcome = resume.run()

    assert resume.deployed == []
    assert resume.ssm.cell_runs == 0
    assert outcome.pointers == first.pointers


def test_one_points_knob_change_redeploys_and_re_runs_only_that_point(
    tmp_path: Path,
) -> None:
    # Two engine points: caching on (two cells) and caching off (one cell).
    grid = _GRID.replace(
        "      prefix_share: [10, 50]\n",
        "      prefix_share: [10, 50]\n"
        '    "off":\n'
        "      flag: --no-enable-prefix-caching\n"
        "      prefix_share: [0]\n",
    )
    task = _isolated_task(tmp_path)
    _Sweep(tmp_path, task, grid=grid).run(every_point=True)

    # Only the caching-on point's engine args change.
    edited = grid.replace(
        "flag: --enable-prefix-caching", "flag: --enable-prefix-caching --x"
    )
    changed = _Sweep(tmp_path, task, grid=edited)
    (off_point,) = changed.other_points
    assert changed.is_pending() is True
    assert changed.is_pending(off_point) is False
    changed.run(every_point=True)

    assert changed.deployed == [changed.point]
    assert changed.ssm.cell_runs == 2  # the caching-on point's two cells, none of off's
