"""The kv-cache-extension-ab driver: the KV cache offload A/B's interleaved runs (#326).

:func:`drive_kv_ab` is the transport-free loop. It alternates the two arms' runs — offload
off, then on, ``runs_per_arm`` times — so a drift over the session lands on both arms
alike. Each run redeploys the GPU with its arm's offload switch, which clears both cache
tiers; scrapes the GPU KV pool the engine sized at startup and refuses a working set the
pool holds or the CPU tier cannot; warms both tiers with one unrecorded cell; then runs
the concurrency ladder. Its collaborators are injected as callables, so the whole path is
testable against fakes with no cluster, GPU, or Prefect server. A run whose cells are all
cached skips its redeploy; any other run is redone whole.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from slipstream_bench.contract.kv_ab_config import KvAbConfig
from slipstream_bench.orchestration.kv_ab_rig import (
    KvAbRig,
    check_fits_context,
    check_working_set,
)

Arm = Literal["off", "on"]

# The arms in the order each repetition runs them.
_ARMS: tuple[Arm, ...] = ("off", "on")


@dataclass(frozen=True)
class ArmRun:
    """One run of one arm: a redeploy, a warmup, and the full ladder.

    :param arm: whether the run offloads KV cache to LMCache's CPU tier.
    :param run: the run's 1-based repetition within its arm.
    """

    arm: Arm
    run: int

    @property
    def kv_cache_offload(self) -> bool:
        """Whether the run's GPU is deployed with the LMCache connector."""
        return self.arm == "on"

    def slug(self) -> str:
        """Name the run as its results subdir does: ``<arm>-r<run>``."""
        return f"{self.arm}-r{self.run}"


@dataclass(frozen=True)
class KvAbCollaborators:
    """The in-cluster and per-cell callables the driver sequences.

    :param deploy: redeploy the GPU with the run's offload switch.
    :param scrape_gpu_pool: read the GPU KV pool size, in tokens, off the startup log.
    :param warm_up: run the run's unrecorded warmup cell.
    :param run_cell: run one ladder rung of a run, re-measuring it when ``refresh`` is
        set, and return its result's S3 pointer.
    :param has_pending_cells: whether any of the run's cells lacks a cached result.
    """

    deploy: Callable[[ArmRun], None]
    scrape_gpu_pool: Callable[[], int]
    warm_up: Callable[[ArmRun], None]
    run_cell: Callable[[ArmRun, int, bool], str]
    has_pending_cells: Callable[[ArmRun], bool]


@dataclass(frozen=True)
class KvAbOutcome:
    """The A/B's result: its cell pointers and how many runs ran or resumed.

    :param pointers: the S3 pointer of every ladder cell, in run order.
    :param runs_run: the runs redeployed and measured.
    :param runs_resumed: the runs whose every cell was read from cache.
    """

    pointers: tuple[str, ...]
    runs_run: int
    runs_resumed: int


def drive_kv_ab(
    config: KvAbConfig, rig: KvAbRig, collaborators: KvAbCollaborators
) -> KvAbOutcome:
    """Run every arm's runs, interleaved, each a redeploy, warmup, and ladder.

    The context window is checked before the first redeploy; the working set after each,
    against the pool the engine sized at startup. Resume is per whole run: a redeploy
    clears both cache tiers, so a ladder measured on one deploy cannot be finished on
    another. A run whose every cell is cached skips its redeploy and reads its cells from
    cache; any other run is redeployed and re-measures every cell.

    :param config: the A/B workload.
    :param rig: the rig capacities read off the manifest.
    :param collaborators: the callables the runs are sequenced through.
    :return: the outcome — the cell pointers and the run/resume split.
    :raise WorkingSetError: when a request overruns max-model-len, or the working set
        fits the GPU pool or overflows the CPU tier.
    """
    check_fits_context(config, rig)
    pointers: list[str] = []
    runs_run = 0
    arm_runs = list_arm_runs(config.runs_per_arm)
    for arm_run in arm_runs:
        pending = collaborators.has_pending_cells(arm_run)
        if pending:
            _prepare_run(arm_run, config, rig, collaborators)
            runs_run += 1
        pointers.extend(
            collaborators.run_cell(arm_run, concurrency, pending)
            for concurrency in config.max_concurrency
        )
    return KvAbOutcome(
        pointers=tuple(pointers),
        runs_run=runs_run,
        runs_resumed=len(arm_runs) - runs_run,
    )


def _prepare_run(
    arm_run: ArmRun, config: KvAbConfig, rig: KvAbRig, collaborators: KvAbCollaborators
) -> None:
    """Redeploy for the run's arm, guard the working set, and warm both tiers."""
    collaborators.deploy(arm_run)
    gpu_pool_tokens = collaborators.scrape_gpu_pool()
    check_working_set(config, rig, gpu_pool_tokens=gpu_pool_tokens)
    collaborators.warm_up(arm_run)


def list_arm_runs(runs_per_arm: int) -> list[ArmRun]:
    """List the runs in their interleaved order: off-r1, on-r1, off-r2, on-r2, ...

    :param runs_per_arm: the repetitions of each arm.
    :return: every run of both arms, alternating arms within each repetition.
    """
    return [
        ArmRun(arm=arm, run=run) for run in range(1, runs_per_arm + 1) for arm in _ARMS
    ]
