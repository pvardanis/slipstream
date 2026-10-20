"""The kv-cache-extension-ab driver: the KV cache offload A/B's interleaved runs (#326).

:func:`drive_kv_ab` is the transport-free loop. It alternates the two arms' runs — offload
off, then on, ``runs_per_arm`` times — so a drift over the session lands on both arms
alike. Each run redeploys the GPU with its arm's offload switch, which clears the arm's
cache tiers; scrapes the GPU KV pool the engine sized at startup and refuses a working set
the pool holds; warms the arm's cache tiers with one unrecorded cell; then runs the
concurrency ladder. A workload the manifest's capacities or its own configured pool
already rule out is refused before the first redeploy. Its collaborators are injected as
callables, so the whole path is testable against fakes with no cluster, GPU, or Prefect
server. A run whose cells are all cached skips its redeploy; any other run is redone
whole.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from slipstream_bench.contract.kv_ab_config import KvAbConfig
from slipstream_bench.orchestration.kv_ab_rig import (
    KvAbRig,
    check_fits_rig,
    check_gpu_pool,
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

    def __post_init__(self) -> None:
        """Reject a run before the first repetition.

        :raise ValueError: when ``run`` is below 1.
        """
        if self.run < 1:
            raise ValueError(f"ArmRun runs count from 1, got run {self.run}")

    @property
    def kv_cache_offload(self) -> bool:
        """Whether the run's GPU is deployed with the LMCache connector."""
        return self.arm == "on"

    def slug(self) -> str:
        """Name the run as its results subdir does: ``<arm>-r<run>``."""
        return f"{self.arm}-r{self.run}"


# The GPU redeploy with the run's offload switch, sequenced for its rollout.
DeployFn = Callable[[ArmRun], None]

# The GPU KV pool size, in tokens, the deployed engine reported at startup.
PoolScrapeFn = Callable[[], int]

# The run's one unrecorded warmup cell, sequenced for the tiers it fills.
WarmUpFn = Callable[[ArmRun], None]

# One ladder rung of a run: (run, concurrency, refresh) -> the result's S3 pointer. With
# refresh set the rung is re-measured even when a cached result exists.
CellFn = Callable[[ArmRun, int, bool], str]

# Whether any of the run's cells lacks a cached result.
PendingRunFn = Callable[[ArmRun], bool]


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

    deploy: DeployFn
    scrape_gpu_pool: PoolScrapeFn
    warm_up: WarmUpFn
    run_cell: CellFn
    has_pending_cells: PendingRunFn


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

    def __post_init__(self) -> None:
        """Reject a negative run count, which no A/B can tally.

        :raise ValueError: when ``runs_run`` or ``runs_resumed`` is below 0.
        """
        if self.runs_run < 0 or self.runs_resumed < 0:
            raise ValueError(
                f"KvAbOutcome run counts must be non-negative: runs_run "
                f"{self.runs_run}, runs_resumed {self.runs_resumed}"
            )


def drive_kv_ab(
    config: KvAbConfig, rig: KvAbRig, collaborators: KvAbCollaborators
) -> KvAbOutcome:
    """Run every arm's runs, interleaved, each a redeploy, warmup, and ladder.

    The context window, the CPU tier, and the configured GPU pool are checked before the
    first redeploy; the GPU pool again after each, as the engine sized it at startup. Resume
    is per whole run: a redeploy clears the arm's cache tiers, so a ladder measured on one
    deploy cannot be finished on another. A run whose every cell is cached skips its
    redeploy and reads its cells from cache; any other run is redeployed and re-measures
    every cell.

    :param config: the A/B workload.
    :param rig: the rig capacities read off the manifest.
    :param collaborators: the callables the runs are sequenced through.
    :return: the outcome — the cell pointers and the run/resume split.
    :raise KvAbWorkloadError: when a request overruns max-model-len, a prefix is off the
        chunk grid, the prompts overflow the CPU tier, or the GPU pool holds the working
        set or not the top rung's requests.
    """
    check_fits_rig(config, rig)
    pointers: list[str] = []
    runs_run = 0
    arm_runs = list_arm_runs(config.runs_per_arm)
    for arm_run in arm_runs:
        pending = collaborators.has_pending_cells(arm_run)
        if pending:
            _prepare_run(arm_run, config, collaborators)
            runs_run += 1
        # A pending run was redeployed, which cleared its cache tiers, so every rung is
        # re-measured on this deploy; a fully cached run reads every rung from cache.
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
    arm_run: ArmRun, config: KvAbConfig, collaborators: KvAbCollaborators
) -> None:
    """Redeploy for the run's arm, guard the working set, and warm the arm's tiers."""
    collaborators.deploy(arm_run)
    gpu_pool_tokens = collaborators.scrape_gpu_pool()
    check_gpu_pool(config, gpu_pool_tokens=gpu_pool_tokens)
    collaborators.warm_up(arm_run)


def list_arm_runs(runs_per_arm: int) -> list[ArmRun]:
    """List the runs in their interleaved order: off-r1, on-r1, off-r2, on-r2, ...

    :param runs_per_arm: the repetitions of each arm.
    :return: every run of both arms, alternating arms within each repetition.
    """
    return [
        ArmRun(arm=arm, run=run) for run in range(1, runs_per_arm + 1) for arm in _ARMS
    ]
