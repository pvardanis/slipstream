"""The point sweep flow: start the proxy once, run one resumable task per cell.

ADR-0012 §Amendment: the Tier-2 grid loop lives in the orchestration layer, and each
cell is one ``docker run`` on the bench host over SSM. :func:`run_point_sweep` drives
**one engine point**'s cells (the Tier-1 GPU redeploy stays in the justfile, ADR-0012
§"Scope boundaries"): it brings the loopback mTLS proxy up once, derives the point's
cells from ``sweep-grid.yaml`` (the single source of the grid and load knobs), and runs
each through the cell task keyed
``run-id:digest:point-slug:cell-name`` so an interrupted run re-runs only the cells that
do not already hold a valid measurement, and a new run id re-measures every cell.

Retry classification (``distributed-ml-patterns.md`` §5): a transient transport fault
(SSM throttle, network flap, spot reclaim, OOM) is what the cell task's opt-in
``retries`` is for; a permanent fault (a degenerate result, a bad image) raises through
the validity gate uncached and is re-attempted on the next whole-sweep run, not retried
in place. The proxy is a shared once-per-flow resource: a fully-resumed run still brings
it up (cheap, idempotent) even though every cached cell skips its SSM side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from prefect import Task, flow, tags

from slipstream_bench.contract import (
    CellConfig,
    EnginePoint,
    SweepGrid,
    build_point_sweep_config,
    get_cell_basename,
    load_grid,
)
from slipstream_bench.orchestration.cell_run import (
    PLACEHOLDER_BASE_URL,
    PLACEHOLDER_OUT_DIR,
    CellExecutionContext,
    build_cell_execution,
    get_cell_knobs,
    get_cell_result_uri,
)
from slipstream_bench.orchestration.digest import (
    ServingConfig,
    config_digest,
    read_sweep_flags,
)
from slipstream_bench.orchestration.grid import get_engine_args
from slipstream_bench.orchestration.ssm import run_command
from slipstream_bench.orchestration.task_labels import get_cell_run_tags
from slipstream_bench.orchestration.validity import DEFAULT_MAX_ERROR_RATE

# The host script that brings the loopback mTLS proxy up (dropped at boot). Run once
# per flow, before any cell, so the whole point's ladder shares one proxy.
_PROXY_SCRIPT = "/usr/local/bin/bench-proxy-up.sh"


@dataclass(frozen=True)
class SweepContext:
    """The per-run context one point sweep runs against.

    ``run_id`` is the point-nested bucket prefix (``<run>/<point-slug>``) the cell JSON
    lands under, matching ``bench-sweep.sh``'s ``s3://<bucket>/sweeps/<run_id>/`` copy;
    ``point_slug`` is the engine point's cache-key part; ``serving`` is the run-wide model
    and vLLM image each cell's config digest folds in (:func:`get_cell_digest`).
    """

    run_id: str
    point_slug: str
    serving: ServingConfig
    instance_id: str
    image_ref: str
    bucket: str
    model: str
    commercial: bool = False
    sweep_args_b64: str = ""
    proxy_timeout_s: float = 240.0
    cell_timeout_s: float = 3600.0

    def __post_init__(self) -> None:
        """Reject a context that cannot address a run or would stall the transport.

        Frozen, so validating on construction makes the instance valid for its whole
        life: the identifiers that key the cache and address S3 must be non-empty,
        ``run_id`` must nest ``point_slug`` (the cells land under it), both timeouts
        must be positive so the SSM poll loop has a deadline to trip, and the extra
        load-cell flags must be readable, since every cell's digest folds them in.

        :raise DigestError: when the extra load-cell flags cannot be read.
        """
        for name in (
            "run_id",
            "point_slug",
            "instance_id",
            "image_ref",
            "bucket",
            "model",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"SweepContext.{name} must be a non-empty string")
        if self.point_slug not in self.run_id:
            raise ValueError(
                f"SweepContext.run_id {self.run_id!r} must nest point_slug "
                f"{self.point_slug!r}: the run's cell objects land under it"
            )
        for name in ("proxy_timeout_s", "cell_timeout_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"SweepContext.{name} must be positive")
        read_sweep_flags(self.sweep_args_b64)

    @property
    def run_group(self) -> str:
        """The shared run every point in one sweep groups under in Prefect.

        ``run_id`` is ``<run>/<point-slug>``; the leading ``<run>`` is the knob sweep's
        single run, and every point tags its flow run with it so the UI filters the
        whole sweep — all its engine points — as one group.
        """
        return self.run_id.rsplit("/", 1)[0]


def run_point_sweep(
    *,
    grid_path: Path,
    context: SweepContext,
    ssm_client: Any,
    s3_client: Any,
    task: Task[..., str],
    proxy_command: str = _PROXY_SCRIPT,
    poll_interval_s: float = 5.0,
    sleep: Callable[[float], None] | None = None,
) -> list[str]:
    """Run one engine point's Tier-2 cells as resumable tasks, returning their pointers.

    :param grid_path: the ``sweep-grid.yaml`` the point's cells are derived from — the
        single source of the grid axes, load knobs, and engine args each cell's digest
        folds in.
    :param context: the per-run context (run id, point slug, serving config, host,
        bucket).
    :param ssm_client: the boto3 SSM client (or stand-in) commands are sent through.
    :param s3_client: the boto3 S3 client (or stand-in) results are downloaded with.
    :param task: the cell task (``cell_task`` in production, a tmp-storage task in
        tests) that caches, gates, and returns each cell's pointer.
    :param proxy_command: the host command that brings the loopback proxy up.
    :param poll_interval_s: the wait between SSM invocation polls.
    :param sleep: the wait function, injected for tests.
    :return: the S3 pointer for each cell, in enumeration order.
    """

    # The @flow wraps a zero-argument closure, not run_point_sweep itself, so the
    # injected collaborators — the boto3 SSM/S3 clients, the cell Task, the sleep
    # callable — stay out of the flow's parameter set. Prefect runs every flow
    # parameter through serialize_parameters (FastAPI jsonable_encoder) to persist the
    # flow-run record; a live boto3 client (sockets, locks) does not survive that.
    # Only the frozen SweepContext is captured by value, addressed through the closure.
    @flow(name="point-sweep", flow_run_name=context.point_slug)
    def _flow() -> list[str]:
        return _drive_point_sweep(
            grid_path=grid_path,
            context=context,
            ssm_client=ssm_client,
            s3_client=s3_client,
            task=task,
            proxy_command=proxy_command,
            poll_interval_s=poll_interval_s,
            sleep=sleep,
        )

    # Name each point's flow run for its slug and tag it with the shared run, so the UI
    # groups the whole knob sweep — every engine point — under one filterable run.
    with tags(f"run={context.run_group}"):
        return _flow()


def _drive_point_sweep(
    *,
    grid_path: Path,
    context: SweepContext,
    ssm_client: Any,
    s3_client: Any,
    task: Task[..., str],
    proxy_command: str,
    poll_interval_s: float,
    sleep: Callable[[float], None] | None,
) -> list[str]:
    """Bring the proxy up once, then run each enumerated cell through the task."""
    proxy_kwargs: dict[str, Any] = {
        "instance_id": context.instance_id,
        "command": proxy_command,
        "timeout_s": context.proxy_timeout_s,
        "poll_interval_s": poll_interval_s,
    }
    if sleep is not None:
        proxy_kwargs["sleep"] = sleep
    run_command(ssm_client, **proxy_kwargs)

    grid = load_grid(grid_path)
    execution_context = CellExecutionContext(
        ssm_client=ssm_client,
        s3_client=s3_client,
        instance_id=context.instance_id,
        image_ref=context.image_ref,
        bucket=context.bucket,
        model=context.model,
        run_id=context.run_id,
        sweep_args_b64=context.sweep_args_b64,
        timeout_s=context.cell_timeout_s,
        poll_interval_s=poll_interval_s,
        sleep=sleep,
    )

    # Each cell's result JSON is downloaded only so the validity gate can read it, then
    # discarded — the durable copy stays in S3. The worker runs read-only-rootfs, so the
    # download target is an ephemeral dir (writable /tmp), torn down when the point ends.
    pointers: list[str] = []
    with TemporaryDirectory() as scratch:
        for cell in list_point_cells(grid, context):
            name = get_cell_basename(cell)
            dest = Path(scratch) / name
            execute = build_cell_execution(cell, context=execution_context, dest=dest)
            labeled = task.with_options(
                tags=get_cell_run_tags(context.point_slug, cell)
            )
            pointers.append(
                labeled(
                    run_id=context.run_id,
                    digest=get_cell_digest(grid, context, cell),
                    point_slug=context.point_slug,
                    cell_name=name,
                    execute_func=execute,
                    result_path=dest,
                    result_uri=get_cell_result_uri(
                        context.bucket, context.run_id, cell
                    ),
                )
            )
    return pointers


def list_point_cells(grid: SweepGrid, context: SweepContext) -> list[CellConfig]:
    """Enumerate one engine point's cells from the grid, their single source.

    The point sweep runs these and the redeploy-skip gate checks them, so both address the
    same cells.

    :param grid: the validated knob-sweep grid.
    :param context: the point's context (its slug, served model, and commercial arm).
    :return: the point's cells, in the grid's cell order.
    :raise SweepGridError: when the point's knobs are not an arm this grid sweeps.
    """
    sweep_config = build_point_sweep_config(
        grid,
        context.point_slug,
        base_url=PLACEHOLDER_BASE_URL,
        model=context.model,
        out_dir=PLACEHOLDER_OUT_DIR,
        commercial=context.commercial,
    )
    return list(sweep_config.cells())


def get_cell_digest(grid: SweepGrid, context: SweepContext, cell: CellConfig) -> str:
    """Compute one cell's config digest, the cache-key part naming its config identity.

    The point sweep keys each cell task by this digest and the redeploy-skip gate looks the
    cell up by it, so the two agree on which cells hold a record. It folds in this point's
    engine args (as the redeploy renders them) and the cell's shipped knobs — not the whole
    grid file — plus the run-wide model, vLLM and bench-client images, the extra load-cell
    flags, and the validity threshold the cell's result is gated on.

    :param grid: the validated knob-sweep grid the point's engine args are resolved from.
    :param context: the point's context.
    :param cell: one of the point's cells (from :func:`list_point_cells`).
    :return: the cell's 64-char hex config digest.
    """
    point = EnginePoint.from_dirname(context.point_slug)
    return config_digest(
        model_yaml=context.serving.model_yaml,
        point_knobs=asdict(get_engine_args(grid, point)),
        cell_params=get_cell_knobs(cell),
        vllm_image_ref=context.serving.vllm_image_ref,
        bench_image_ref=context.image_ref,
        sweep_flags=read_sweep_flags(context.sweep_args_b64),
        max_error_rate=DEFAULT_MAX_ERROR_RATE,
    )
