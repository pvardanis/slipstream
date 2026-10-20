"""Tests for the kv-cache-extension-ab driver, against fake collaborators.

The driver interleaves the arms' runs (off, on, off, on, ...), and each pending run
redeploys the GPU with its arm's offload switch, scrapes the GPU KV pool, guards the
working set against it, warms the arm's cache tiers with one unrecorded cell, then runs the
concurrency ladder. The fakes record each call, so the order is asserted as a script.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from slipstream_bench.orchestration.flows.kv_ab import (
    ArmRun,
    KvAbCollaborators,
    KvAbResult,
    drive_kv_ab,
)
from slipstream_bench.orchestration.kv_ab_config import load_kv_ab_config
from slipstream_bench.orchestration.kv_ab_rig import KvAbRig, KvAbWorkloadError

_CONFIG = load_kv_ab_config(
    Path(__file__).parents[2] / "bench" / "kv-cache-extension-ab.agent.yaml"
)
# The committed manifest's rig: a 4096-token window, a 4.0 GiB tier, 256-token chunks.
_RIG = KvAbRig(max_model_len=4096, cpu_tier_bytes=4 * 1024**3, chunk_tokens=256)
# The pool the agent config's kv_cache_memory_bytes sets: 16,384 fp8 tokens.
_CONFIGURED_POOL = 16_384


@dataclass
class _FakeCluster:
    """Records every collaborator call; reports pool sizes in turn, and the cached runs."""

    gpu_pool_tokens: list[int] = field(default_factory=lambda: [_CONFIGURED_POOL] * 6)
    cached_runs: frozenset[str] = frozenset()
    calls: list[str] = field(default_factory=list)

    def collaborators(self) -> KvAbCollaborators:
        return KvAbCollaborators(
            deploy=self._deploy,
            scrape_gpu_pool=self._scrape_gpu_pool,
            warm_up=self._warm_up,
            run_cell=self._run_cell,
            has_pending_cells=self._has_pending_cells,
        )

    def _deploy(self, arm_run: ArmRun) -> None:
        offload = "offload" if arm_run.kv_cache_offload else "no-offload"
        self.calls.append(f"deploy {arm_run.slug()} {offload}")

    def _scrape_gpu_pool(self) -> int:
        self.calls.append("scrape")
        return self.gpu_pool_tokens.pop(0)

    def _warm_up(self, arm_run: ArmRun) -> None:
        self.calls.append(f"warmup {arm_run.slug()}")

    def _run_cell(self, arm_run: ArmRun, concurrency: int, refresh: bool) -> str:
        self.calls.append(f"cell {arm_run.slug()} c{concurrency} refresh={refresh}")
        return f"s3://bucket/kv-ab/run/{arm_run.slug()}/c{concurrency}.json"

    def _has_pending_cells(self, arm_run: ArmRun) -> bool:
        return arm_run.slug() not in self.cached_runs


def _run_script(slug: str, offload: str) -> list[str]:
    """The calls one pending run makes: redeploy, scrape, warm up, then the ladder."""
    return [
        f"deploy {slug} {offload}",
        "scrape",
        f"warmup {slug}",
        *(f"cell {slug} c{c} refresh=True" for c in (1, 2, 4)),
    ]


def test_the_arms_interleave_and_each_run_redeploys_warms_up_then_climbs_the_ladder() -> (
    None
):
    """Three runs per arm alternate off, on; a drift over time hits both arms alike."""
    cluster = _FakeCluster()

    drive_kv_ab(_CONFIG, _RIG, cluster.collaborators())

    assert cluster.calls == [
        *_run_script("off-r1", "no-offload"),
        *_run_script("on-r1", "offload"),
        *_run_script("off-r2", "no-offload"),
        *_run_script("on-r2", "offload"),
        *_run_script("off-r3", "no-offload"),
        *_run_script("on-r3", "offload"),
    ]


def test_a_request_past_max_model_len_is_refused_before_any_redeploy() -> None:
    """A context overrun fails every cell alike, so no GPU minute is spent on it."""
    cluster = _FakeCluster()
    rig = KvAbRig(max_model_len=3700, cpu_tier_bytes=4 * 1024**3, chunk_tokens=256)

    with pytest.raises(KvAbWorkloadError, match="max-model-len"):
        drive_kv_ab(_CONFIG, rig, cluster.collaborators())

    assert cluster.calls == []


def test_prompts_past_the_cpu_tier_are_refused_before_any_redeploy() -> None:
    """The tier is read off the manifest, so its overflow needs no GPU to find."""
    cluster = _FakeCluster()
    rig = KvAbRig(max_model_len=4096, cpu_tier_bytes=1024**3, chunk_tokens=256)

    with pytest.raises(KvAbWorkloadError, match="CPU tier"):
        drive_kv_ab(_CONFIG, rig, cluster.collaborators())

    assert cluster.calls == []


def test_a_working_set_the_gpu_pool_holds_is_refused_before_any_cell() -> None:
    """The pool is known only once the engine starts, so the first run stops there."""
    cluster = _FakeCluster(gpu_pool_tokens=[300_000])

    with pytest.raises(KvAbWorkloadError, match="working set fits"):
        drive_kv_ab(_CONFIG, _RIG, cluster.collaborators())

    assert cluster.calls == ["deploy off-r1 no-offload", "scrape"]


def test_a_fully_cached_run_skips_its_redeploy_and_reads_its_cells_from_cache() -> None:
    """off-r1 and on-r1 hold every cell: no redeploy, no warmup, no re-measure."""
    cluster = _FakeCluster(cached_runs=frozenset({"off-r1", "on-r1"}))

    result = drive_kv_ab(_CONFIG, _RIG, cluster.collaborators())

    cached = [
        f"cell {slug} c{c} refresh=False"
        for slug in ("off-r1", "on-r1")
        for c in (1, 2, 4)
    ]
    assert cluster.calls[: len(cached)] == cached
    assert cluster.calls[len(cached)] == "deploy off-r2 no-offload"
    assert (result.runs_run, result.runs_resumed) == (4, 2)


def test_the_result_carries_every_cell_pointer_in_run_order() -> None:
    """Six runs of a three-rung ladder are 18 pointers, off-r1's ladder first."""
    cluster = _FakeCluster()

    result = drive_kv_ab(_CONFIG, _RIG, cluster.collaborators())

    assert len(result.pointers) == 18
    assert result.pointers[:2] == (
        "s3://bucket/kv-ab/run/off-r1/c1.json",
        "s3://bucket/kv-ab/run/off-r1/c2.json",
    )
    assert result.pointers[-1] == "s3://bucket/kv-ab/run/on-r3/c4.json"


def test_a_later_run_whose_pool_holds_the_working_set_stops_before_its_cells() -> None:
    """Each redeploy re-sizes the pool, so on-r1 is guarded on its own startup's size."""
    cluster = _FakeCluster(gpu_pool_tokens=[_CONFIGURED_POOL, 300_000])

    with pytest.raises(KvAbWorkloadError, match="fits the 300000-token GPU KV pool"):
        drive_kv_ab(_CONFIG, _RIG, cluster.collaborators())

    assert cluster.calls == [
        *_run_script("off-r1", "no-offload"),
        "deploy on-r1 offload",
        "scrape",
    ]


def test_an_arm_run_before_the_first_repetition_is_rejected() -> None:
    """Runs count from 1, as their ``<arm>-r<run>`` results subdirs do."""
    with pytest.raises(ValueError, match="run 0"):
        ArmRun(arm="off", run=0)


@pytest.mark.parametrize(("runs_run", "runs_resumed"), [(-1, 1), (1, -1)])
def test_a_result_with_a_negative_run_count_is_rejected(
    runs_run: int, runs_resumed: int
) -> None:
    """A run count is a tally of runs; below zero it misreports the A/B."""
    with pytest.raises(ValueError, match="non-negative"):
        KvAbResult(pointers=(), runs_run=runs_run, runs_resumed=runs_resumed)
