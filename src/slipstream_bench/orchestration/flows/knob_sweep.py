"""The parent knob-sweep flow: one point sweep per engine point (ADR-0015).

The whole outer loop as one Prefect flow. It iterates the grid's engine points and, per
point, redeploys the GPU, scrapes the concurrency ceiling, then runs the existing
per-point :func:`slipstream_bench.orchestration.flows.point_sweep.run_point_sweep` as a nested
subflow — the per-point flow is wrapped, not rewritten. A resume skips a point whose
cells already hold valid measurements (the ~20-minute GPU redeploy is not re-paid to run
zero cells), and a ceiling scrape that finds nothing raises loudly rather than running
the point's ladder against a garbage ceiling.

The collaborators — the GPU redeploy, the ceiling scrape, the per-point sweep, and the
redeploy-skip predicate — are injected as callables into :func:`drive_knob_sweep`, which
only sequences them, so the whole path is testable against fakes with no cluster, GPU, or
Prefect server. Production wraps it in the ``@flow``-decorated ``knob_sweep_flow`` at the
composition root (the ``slipstream-orchestrate`` CLI), which builds the real collaborators
and is the entrypoint the Prefect deployment registers. Each point sweep runs nested under
that one parent flow run, so the point sweeps of a knob sweep share a parent->child lineage
in the Prefect UI.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from slipstream_bench.sweep.aggregation import EnginePoint

# The GPU redeploy for one engine point's knobs (Tier-1). Returns nothing: the flow
# sequences it for its side effect, the rollout of ``deploy/vllm-gpu``.
DeployFn = Callable[[EnginePoint], None]

# The concurrency-ceiling scrape run after a redeploy. Returns nothing: the flow sequences
# it for its raise-on-empty side effect and raises CeilingScrapeError when the engine
# reported none (ADR-0015 fail-loud). The grid fixes the ladder shape, so the scraped
# value is recorded by the task, not threaded through the flow.
ScrapeFn = Callable[[EnginePoint], None]

# One engine point's Tier-2 sweep (``run_point_sweep`` in production), returning the S3
# pointer for each of its cells.
PointSweepFn = Callable[[EnginePoint], list[str]]

# The redeploy-skip gate: whether the point still has cells to run (ADR-0015). False
# when every cell already holds a valid measurement, so the point is skipped entirely.
PendingCellsFn = Callable[[EnginePoint], bool]


def drive_knob_sweep(
    *,
    points: Sequence[EnginePoint],
    deploy_fn: DeployFn,
    scrape_fn: ScrapeFn,
    point_sweep_fn: PointSweepFn,
    has_pending_cells: PendingCellsFn,
) -> list[str]:
    """Iterate the points, redeploying and scraping each pending one, then sweeping all.

    The redeploy and scrape — the ~20-minute GPU rollout and its ceiling read — are gated
    on the point having pending cells, so a resume never re-pays them to run zero cells
    (ADR-0015). The sweep runs for every point regardless: for a fully-cached point it
    hits the cache and re-executes nothing, returning the point's cached pointers.

    :param points: the grid's engine points, in enumeration order.
    :param deploy_fn: redeploy the GPU for a point's knobs (Tier-1).
    :param scrape_fn: scrape the concurrency ceiling after a redeploy; raises when the
        engine reported none, so the point's ladder never runs against a garbage ceiling.
    :param point_sweep_fn: run one point's Tier-2 cells as a nested subflow.
    :param has_pending_cells: whether a point still has cells to run; a point with none
        skips its redeploy and scrape — the two expensive tasks — but still runs its
        resumable sweep, which returns its cached pointers (resume).
    :return: the S3 pointer for each cell of every point, in order.
    """
    pointers: list[str] = []
    for point in points:
        if has_pending_cells(point):
            deploy_fn(point)
            # Scrape for its raise-on-empty side effect: a point whose engine reported no
            # ceiling must fail before its ladder runs (ADR-0015). The grid fixes the
            # ladder shape, so the scraped value is recorded by the task, not threaded here.
            scrape_fn(point)
        pointers.extend(point_sweep_fn(point))
    return pointers
