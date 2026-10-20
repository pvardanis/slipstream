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

:func:`build_kv_ab_collaborators` binds those callables to kubectl, SSM, S3, and the
engine's /metrics, and :func:`kv_ab_flow` is the ``@flow`` composition root the Prefect
deployment registers: it builds the live transports inside the flow and drives the loop,
so only serializable parameters cross the flow boundary.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from prefect import Task, flow
from prefect.logging import get_run_logger

from slipstream_bench.contract import CellConfig, get_cell_basename
from slipstream_bench.contract.kv_ab_record import Arm
from slipstream_bench.orchestration.cell_run import (
    CellExecutionContext,
    build_cell_execution,
    build_s3_client,
    start_bench_proxy,
)
from slipstream_bench.orchestration.cluster import (
    Kubectl,
    build_kubectl,
    deploy_gpu,
    scrape_gpu_kv_pool,
)
from slipstream_bench.orchestration.digest import ServingConfig, read_serving_config
from slipstream_bench.orchestration.flows.knob_sweep import (
    enable_milestone_logging,
    tag_run_with_group,
)
from slipstream_bench.orchestration.kv_ab_cell import (
    fetch_metrics,
    get_kv_ab_cell,
    get_kv_ab_cell_digest,
    get_kv_ab_record_key,
    get_kv_ab_run_id,
    write_kv_ab_record,
)
from slipstream_bench.orchestration.kv_ab_config import (
    KvAbConfig,
    get_kv_ab_engine_args,
    load_kv_ab_config,
)
from slipstream_bench.orchestration.kv_ab_rig import (
    KvAbRig,
    check_fits_rig,
    check_gpu_kv_pool,
    read_kv_ab_rig,
)
from slipstream_bench.orchestration.model_config import read_model_id
from slipstream_bench.orchestration.ssm import build_ssm_client
from slipstream_bench.orchestration.tasks.cell import is_cell_cached
from slipstream_bench.orchestration.tasks.kv_ab_cell import (
    MetricsReader,
    kv_ab_cell_task,
)
from slipstream_bench.orchestration.validity import validate_cell

_LOGGER = logging.getLogger(__name__)

# The GPU engine's /metrics, read by the in-cluster worker through the vLLM Service.
_ENGINE_METRICS_URL = "http://vllm-gpu.slipstream.svc:8000/metrics"

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
class KvAbResult:
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
                f"KvAbResult run counts must be non-negative: runs_run "
                f"{self.runs_run}, runs_resumed {self.runs_resumed}"
            )


@dataclass(frozen=True)
class KvAbInputs:
    """The per-A/B inputs every arm run shares.

    :param config: the A/B workload.
    :param manifest_text: the raw ``k8s/vllm-gpu.yaml`` text each redeploy renders.
    :param run: the A/B's run id, which every arm run's cells and records nest under.
    :param serving: the model and vLLM serving image every rung's digest folds in.
    :param instance_id: the bench host the cells run on.
    :param image_ref: the bench-client image the cells run.
    :param bucket: the results bucket.
    :param model: the served model id the cells measure and the counters are labelled by.
    :param proxy_timeout_s: the ceiling on the bench proxy's SSM command.
    :param cell_timeout_s: the ceiling on each cell's SSM command.
    """

    config: KvAbConfig
    manifest_text: str
    run: str
    serving: ServingConfig
    instance_id: str
    image_ref: str
    bucket: str
    model: str
    proxy_timeout_s: float = 240.0
    cell_timeout_s: float = 3600.0

    def __post_init__(self) -> None:
        """Reject inputs that would address the A/B's cells with an empty key.

        :raise ValueError: when an addressing field is blank.
        """
        for name in ("run", "instance_id", "image_ref", "bucket", "model"):
            if not getattr(self, name).strip():
                raise ValueError(f"KvAbInputs.{name} must be a non-empty string")


@flow(name="kv-cache-extension-ab")
def kv_ab_flow(
    *,
    run_id: str,
    instance_id: str,
    region: str,
    bucket: str,
    image_ref: str,
    model_yaml: Path,
    kv_ab_config: Path,
    vllm_manifest: Path,
    metrics_url: str = _ENGINE_METRICS_URL,
    retries: int = 0,
) -> list[str]:
    """Run the KV cache offload A/B as one Prefect flow and return its record pointers.

    The composition root the deployment registers: it reads the workload, the rig, and
    the run-wide digest inputs, binds the driver's collaborators to the live transports,
    and drives the interleaved arm runs. Re-invoked with the same run id, it resumes every
    run whose rungs are all cached. The parameters are all serializable; the three paths
    name in-image files read on the worker. The run is tagged ``run=<run-id>``.

    :param run_id: the A/B's run id, which its cells and records nest under.
    :param instance_id: the bench host the cells run on.
    :param region: the AWS region of the host and bucket.
    :param bucket: the results bucket (RESULTS_BUCKET).
    :param image_ref: the bench-client image the cells run.
    :param model_yaml: model.yaml, the served model id and a digest input.
    :param kv_ab_config: the A/B workload, e.g. the agent or chat config.
    :param vllm_manifest: k8s/vllm-gpu.yaml, the redeploy template and the rig's
        capacities.
    :param metrics_url: the GPU engine's /metrics URL, reachable from the worker.
    :param retries: opt-in cell retries for a transient transport fault.
    :return: the record pointer of every ladder rung, in run order.
    """
    logger = get_run_logger()
    enable_milestone_logging()
    tag_run_with_group(run_id)
    config = load_kv_ab_config(kv_ab_config)
    logger.info(
        "kv-ab run=%s starting: %s, %d runs per arm",
        run_id,
        kv_ab_config.name,
        config.runs_per_arm,
    )
    inputs = KvAbInputs(
        config=config,
        manifest_text=vllm_manifest.read_text(encoding="utf-8"),
        run=run_id,
        serving=read_serving_config(model_yaml=model_yaml, vllm_manifest=vllm_manifest),
        instance_id=instance_id,
        image_ref=image_ref,
        bucket=bucket,
        model=read_model_id(model_yaml),
    )
    collaborators = build_kv_ab_collaborators(
        inputs,
        kubectl=build_kubectl(),
        ssm_client=build_ssm_client(region),
        s3_client=build_s3_client(region),
        task=kv_ab_cell_task(bucket, retries=retries),
        read_metrics=partial(fetch_metrics, metrics_url),
    )
    result = drive_kv_ab(config, read_kv_ab_rig(vllm_manifest), collaborators)
    logger.info(
        "kv-ab run=%s complete: %d cells (%d runs run, %d resumed)",
        run_id,
        len(result.pointers),
        result.runs_run,
        result.runs_resumed,
    )
    return list(result.pointers)


def drive_kv_ab(
    config: KvAbConfig, rig: KvAbRig, collaborators: KvAbCollaborators
) -> KvAbResult:
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
    :return: the result — the cell pointers and the run/resume split.
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
    return KvAbResult(
        pointers=tuple(pointers),
        runs_run=runs_run,
        runs_resumed=len(arm_runs) - runs_run,
    )


def list_arm_runs(runs_per_arm: int) -> list[ArmRun]:
    """List the runs in their interleaved order: off-r1, on-r1, off-r2, on-r2, ...

    :param runs_per_arm: the repetitions of each arm.
    :return: every run of both arms, alternating arms within each repetition.
    """
    return [
        ArmRun(arm=arm, run=run) for run in range(1, runs_per_arm + 1) for arm in _ARMS
    ]


def build_kv_ab_collaborators(
    inputs: KvAbInputs,
    *,
    kubectl: Kubectl,
    ssm_client: Any,
    s3_client: Any,
    task: Task[..., str],
    read_metrics: MetricsReader,
) -> KvAbCollaborators:
    """Bind the driver's collaborators to the live transports.

    The redeploy renders the A/B's engine args and the run's offload switch into the
    manifest; the pool scrape reads the engine's startup log. The warmup brings the bench
    proxy up and runs one cell at the warmup concurrency under ``<run id>/warmup``,
    gated but neither cached nor recorded. Each rung runs through the kv-ab cell task,
    keyed by the run's ``kv-ab/<run>/<arm>-r<n>`` run id and ``<arm>-r<n>`` slug, and the
    pending gate reads that task's cache for every rung of the run.

    :param inputs: the per-A/B inputs every arm run shares.
    :param kubectl: the transport the redeploy and pool scrape are issued through.
    :param ssm_client: the boto3 SSM client the proxy and cells are sent through.
    :param s3_client: the boto3 S3 client results are downloaded and records written with.
    :param task: the kv-ab cell task each rung is cached and measured with.
    :param read_metrics: reads the serving engine's /metrics around each rung.
    :return: the collaborators :func:`drive_kv_ab` sequences.
    """
    transports = {"ssm_client": ssm_client, "s3_client": s3_client}
    return KvAbCollaborators(
        deploy=partial(_deploy_arm_run, inputs=inputs, kubectl=kubectl),
        scrape_gpu_pool=partial(_scrape_gpu_pool, kubectl=kubectl),
        warm_up=partial(_warm_up, inputs=inputs, **transports),
        run_cell=partial(
            _run_rung, inputs=inputs, task=task, read_metrics=read_metrics, **transports
        ),
        has_pending_cells=partial(_run_has_pending_cells, inputs=inputs, task=task),
    )


@dataclass(frozen=True)
class _Rung:
    """One rung of one arm run, addressed: its cell, run id, cell name, and digest."""

    cell: CellConfig
    run_id: str
    cell_name: str
    digest: str


def _prepare_run(
    arm_run: ArmRun, config: KvAbConfig, collaborators: KvAbCollaborators
) -> None:
    """Redeploy for the run's arm, guard the working set, and warm the arm's tiers."""
    collaborators.deploy(arm_run)
    gpu_pool_tokens = collaborators.scrape_gpu_pool()
    check_gpu_kv_pool(config, gpu_pool_tokens=gpu_pool_tokens)
    collaborators.warm_up(arm_run)


def _deploy_arm_run(arm_run: ArmRun, *, inputs: KvAbInputs, kubectl: Kubectl) -> None:
    """Redeploy the GPU with the A/B's engine args and the run's offload switch."""
    _LOGGER.info("run %s redeploy starting", arm_run.slug())
    deploy_gpu(
        get_kv_ab_engine_args(inputs.config.engine),
        kv_cache_offload=arm_run.kv_cache_offload,
        manifest_text=inputs.manifest_text,
        kubectl=kubectl,
    )
    _LOGGER.info("run %s redeploy finished", arm_run.slug())


def _scrape_gpu_pool(*, kubectl: Kubectl) -> int:
    """Read the redeployed engine's GPU KV pool size and record it in the log."""
    tokens = scrape_gpu_kv_pool(kubectl=kubectl)
    _LOGGER.info("GPU KV cache pool: %d tokens", tokens)
    return tokens


def _warm_up(
    arm_run: ArmRun, *, inputs: KvAbInputs, ssm_client: Any, s3_client: Any
) -> None:
    """Bring the proxy up and run the run's warmup cell, gated but not recorded.

    :raise InvalidCellError: when the warmup produced no measurement, so the ladder never
        runs against tiers the warmup failed to fill.
    """
    start_bench_proxy(
        ssm_client, instance_id=inputs.instance_id, timeout_s=inputs.proxy_timeout_s
    )
    config = inputs.config
    cell = get_kv_ab_cell(config, config.warmup_concurrency, model=inputs.model)
    run_id = f"{get_kv_ab_run_id(inputs.run, arm_run.slug())}/warmup"
    context = _get_execution_context(inputs, run_id, ssm_client, s3_client)
    with TemporaryDirectory() as scratch:
        dest = Path(scratch) / get_cell_basename(cell)
        build_cell_execution(cell, context=context, dest=dest)()
        validate_cell(dest)
    _LOGGER.info("run %s warmed up", arm_run.slug())


def _run_rung(
    arm_run: ArmRun,
    concurrency: int,
    refresh: bool,
    *,
    inputs: KvAbInputs,
    ssm_client: Any,
    s3_client: Any,
    task: Task[..., str],
    read_metrics: MetricsReader,
) -> str:
    """Run one rung through the kv-ab cell task and return its record's pointer."""
    rung = _get_rung(inputs, arm_run, concurrency)
    context = _get_execution_context(inputs, rung.run_id, ssm_client, s3_client)
    record_key = get_kv_ab_record_key(inputs.run, arm_run.slug(), rung.cell)
    # The download is read only by the validity gate and the record; the durable copies
    # stay in S3, and the worker's root filesystem is read-only, so it lands in /tmp.
    with TemporaryDirectory() as scratch:
        dest = Path(scratch) / rung.cell_name
        return task.with_options(refresh_cache=refresh)(
            run_id=rung.run_id,
            digest=rung.digest,
            point_slug=arm_run.slug(),
            cell_name=rung.cell_name,
            execute_func=build_cell_execution(rung.cell, context=context, dest=dest),
            read_metrics=read_metrics,
            result_path=dest,
            arm=arm_run.arm,
            run=arm_run.run,
            model=inputs.model,
            write_record=partial(
                write_kv_ab_record,
                s3_client=s3_client,
                bucket=inputs.bucket,
                key=record_key,
            ),
        )


def _run_has_pending_cells(
    arm_run: ArmRun, *, inputs: KvAbInputs, task: Task[..., str]
) -> bool:
    """Report whether any rung of the run lacks a cache record, logging the first."""
    for concurrency in inputs.config.max_concurrency:
        rung = _get_rung(inputs, arm_run, concurrency)
        if not is_cell_cached(
            task,
            run_id=rung.run_id,
            digest=rung.digest,
            point_slug=arm_run.slug(),
            cell_name=rung.cell_name,
        ):
            _LOGGER.info(
                "run %s pending: cell %s has no cache record",
                arm_run.slug(),
                rung.cell_name,
            )
            return True
    return False


def _get_rung(inputs: KvAbInputs, arm_run: ArmRun, concurrency: int) -> _Rung:
    """Address one rung of a run: the rung task and the pending gate both read this."""
    cell = get_kv_ab_cell(inputs.config, concurrency, model=inputs.model)
    return _Rung(
        cell=cell,
        run_id=get_kv_ab_run_id(inputs.run, arm_run.slug()),
        cell_name=get_cell_basename(cell),
        digest=get_kv_ab_cell_digest(
            inputs.config, cell, serving=inputs.serving, image_ref=inputs.image_ref
        ),
    )


def _get_execution_context(
    inputs: KvAbInputs, run_id: str, ssm_client: Any, s3_client: Any
) -> CellExecutionContext:
    """Build the context a run's cells execute in, under the given bench-host run id."""
    return CellExecutionContext(
        ssm_client=ssm_client,
        s3_client=s3_client,
        instance_id=inputs.instance_id,
        image_ref=inputs.image_ref,
        bucket=inputs.bucket,
        model=inputs.model,
        run_id=run_id,
        timeout_s=inputs.cell_timeout_s,
    )
