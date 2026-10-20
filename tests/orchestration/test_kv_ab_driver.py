"""Tests for the kv-cache-extension-ab driver, against fake collaborators.

The driver interleaves the arms' runs (off, on, off, on, ...), and each pending run
redeploys the GPU with its arm's offload switch, scrapes the GPU KV pool, guards the
working set against it, warms both tiers with one unrecorded cell, then runs the
concurrency ladder. The fakes record each call, so the order is asserted as a script.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from slipstream_bench.contract.kv_ab_config import load_kv_ab_config
from slipstream_bench.orchestration.flows.kv_ab import (
    ArmRun,
    KvAbCollaborators,
    drive_kv_ab,
)
from slipstream_bench.orchestration.kv_ab_rig import KvAbRig, KvAbWorkloadError

_CONFIG = load_kv_ab_config(
    Path(__file__).parents[2] / "bench" / "kv-cache-extension-ab.yaml"
)
# 16 GiB holds 233,016 fp8 tokens: room for the committed 205,824-token working set.
_RIG = KvAbRig(max_model_len=4096, cpu_tier_bytes=16 * 1024**3)
_G5_XLARGE_POOL = 185_696


@dataclass
class _FakeCluster:
    """Records every collaborator call; reports a pool size and a set of cached runs."""

    gpu_pool_tokens: int = _G5_XLARGE_POOL
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
        return self.gpu_pool_tokens

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
        *(f"cell {slug} c{c} refresh=True" for c in (8, 16, 32, 64)),
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
    rig = KvAbRig(max_model_len=3700, cpu_tier_bytes=16 * 1024**3)

    with pytest.raises(KvAbWorkloadError, match="max-model-len"):
        drive_kv_ab(_CONFIG, rig, cluster.collaborators())

    assert cluster.calls == []


def test_a_working_set_past_the_cpu_tier_is_refused_before_any_redeploy() -> None:
    """The tier is read off the manifest, so its overflow needs no GPU to find."""
    cluster = _FakeCluster()
    rig = KvAbRig(max_model_len=4096, cpu_tier_bytes=4 * 1024**3)

    with pytest.raises(KvAbWorkloadError, match="CPU tier"):
        drive_kv_ab(_CONFIG, rig, cluster.collaborators())

    assert cluster.calls == []


def test_a_working_set_the_gpu_pool_holds_is_refused_before_any_cell() -> None:
    """The pool is known only once the engine starts, so the first run stops there."""
    cluster = _FakeCluster(gpu_pool_tokens=300_000)

    with pytest.raises(KvAbWorkloadError, match="GPU KV pool"):
        drive_kv_ab(_CONFIG, _RIG, cluster.collaborators())

    assert cluster.calls == ["deploy off-r1 no-offload", "scrape"]


def test_a_fully_cached_run_skips_its_redeploy_and_reads_its_cells_from_cache() -> None:
    """off-r1 and on-r1 hold every cell: no redeploy, no warmup, no re-measure."""
    cluster = _FakeCluster(cached_runs=frozenset({"off-r1", "on-r1"}))

    outcome = drive_kv_ab(_CONFIG, _RIG, cluster.collaborators())

    cached = [
        f"cell {slug} c{c} refresh=False"
        for slug in ("off-r1", "on-r1")
        for c in (8, 16, 32, 64)
    ]
    assert cluster.calls[: len(cached)] == cached
    assert cluster.calls[len(cached)] == "deploy off-r2 no-offload"
    assert (outcome.runs_run, outcome.runs_resumed) == (4, 2)


def test_the_outcome_carries_every_cell_pointer_in_run_order() -> None:
    """Six runs of a four-rung ladder are 24 pointers, off-r1's ladder first."""
    cluster = _FakeCluster()

    outcome = drive_kv_ab(_CONFIG, _RIG, cluster.collaborators())

    assert len(outcome.pointers) == 24
    assert outcome.pointers[:2] == (
        "s3://bucket/kv-ab/run/off-r1/c8.json",
        "s3://bucket/kv-ab/run/off-r1/c16.json",
    )
    assert outcome.pointers[-1] == "s3://bucket/kv-ab/run/on-r3/c64.json"
