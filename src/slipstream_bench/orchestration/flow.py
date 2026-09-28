"""The point sweep flow: start the proxy once, run one resumable task per cell.

ADR-0012 §Amendment: the Tier-2 grid loop lives in the orchestration layer, and each
cell is one ``docker run`` on the bench host over SSM. :func:`run_point_sweep` drives
**one engine point**'s cells (the Tier-1 GPU redeploy stays in the justfile, ADR-0012
§"Scope boundaries"): it brings the loopback mTLS proxy up once, derives the point's
cells from ``sweep-grid.yaml`` (the single source of the grid and load knobs), and runs
each through the cell task keyed
``digest:point-slug:cell-name`` so an interrupted run re-runs only the cells that do not
already hold a valid measurement.

Retry classification (``distributed-ml-patterns.md`` §5): a transient transport fault
(SSM throttle, network flap, spot reclaim, OOM) is what the cell task's opt-in
``retries`` is for; a permanent fault (a degenerate result, a bad image) raises through
the validity gate uncached and is re-attempted on the next whole-sweep run, not retried
in place. The proxy is a shared once-per-flow resource: a fully-resumed run still brings
it up (cheap, idempotent) even though every cached cell skips its SSM side effects.

The ``orchestration`` extra Prefect ships in is guarded once, at the
``slipstream-orchestrate`` entry (:mod:`slipstream_bench.orchestration.__main__`),
before this module is imported.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prefect import Task, flow

from slipstream_bench.orchestration.cell_run import (
    CellExecutionContext,
    build_cell_execution,
    get_cell_result_uri,
)
from slipstream_bench.orchestration.ssm import run_command
from slipstream_bench.orchestration.task_labels import get_cell_run_tags
from slipstream_bench.sweep.grid import build_point_sweep_config, load_grid
from slipstream_bench.sweep.runner import get_cell_basename

# The host script that brings the loopback mTLS proxy up (dropped at boot). Run once
# per flow, before any cell, so the whole point's ladder shares one proxy.
_PROXY_SCRIPT = "/usr/local/bin/bench-proxy-up.sh"

# Placeholder base_url: bench-sweep.sh supplies the real --base-url (the loopback
# proxy) as a flag, and render_cell_config strips it, so the value enumerated here is
# never sent — it only satisfies the non-empty CellConfig field.
_PLACEHOLDER_BASE_URL = "http://127.0.0.1:0"


@dataclass(frozen=True)
class SweepContext:
    """The per-run context one point sweep runs against.

    ``run_id`` is the point-nested bucket prefix (``<run>/<point-slug>``) the cell JSON
    lands under, matching ``bench-sweep.sh``'s ``s3://<bucket>/sweeps/<run_id>/`` copy;
    ``point_slug`` is the engine point's cache-key part; ``digest`` is the deep config
    digest shared by every cell in the run (ADR-0012).
    """

    run_id: str
    point_slug: str
    digest: str
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
        ``run_id`` must nest ``point_slug`` (the cells land under it), and both
        timeouts must be positive so the SSM poll loop has a deadline to trip.
        """
        for name in (
            "run_id",
            "point_slug",
            "digest",
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


def run_point_sweep(
    *,
    grid_path: Path,
    results_dir: Path,
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
        single source of the grid axes and load knobs, and the file the digest is over.
    :param results_dir: the local directory each cell's result JSON downloads into
        (the validity gate's input).
    :param context: the per-run context (run id, point slug, digest, host, bucket).
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
    @flow(name="point-sweep")
    def _flow() -> list[str]:
        return _drive_point_sweep(
            grid_path=grid_path,
            results_dir=results_dir,
            context=context,
            ssm_client=ssm_client,
            s3_client=s3_client,
            task=task,
            proxy_command=proxy_command,
            poll_interval_s=poll_interval_s,
            sleep=sleep,
        )

    return _flow()


def _drive_point_sweep(
    *,
    grid_path: Path,
    results_dir: Path,
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

    sweep_config = build_point_sweep_config(
        load_grid(grid_path),
        context.point_slug,
        base_url=_PLACEHOLDER_BASE_URL,
        model=context.model,
        out_dir=str(results_dir),
        commercial=context.commercial,
    )

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

    pointers: list[str] = []
    for cell in sweep_config.cells():
        name = get_cell_basename(cell)
        dest = results_dir / name
        execute = build_cell_execution(cell, context=execution_context, dest=dest)
        labeled = task.with_options(tags=get_cell_run_tags(context.point_slug, cell))
        pointers.append(
            labeled(
                digest=context.digest,
                point_slug=context.point_slug,
                cell_name=name,
                execute_func=execute,
                result_path=dest,
                result_uri=get_cell_result_uri(context.bucket, context.run_id, cell),
            )
        )
    return pointers
