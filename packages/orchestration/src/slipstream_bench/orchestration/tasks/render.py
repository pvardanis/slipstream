"""The knob sweep's terminal result render: fold the whole run and publish its tables (ADR-0018).

After the parent knob-sweep point loop persists every cell to S3, one terminal ``@task``
materializes the whole run from S3, folds it with both aggregators, and publishes the ceiling
and goodput-cliff tables as markdown artifacts on the parent run page. It runs once, at the
end, off the persisted cells — so a materialize or publish failure fails only the render,
leaving the cells intact, and a re-run re-renders idempotently off S3 without re-running a cell.
The task carries no retry budget, so recovery is a re-run (manual or a resumed sweep), not an
automatic Prefect retry.

Three layers mirror the sweep driver (:mod:`slipstream_bench.orchestration.flows.knob_sweep`).
:func:`drive_render_tables` is the transport-free core: it opens a materialized run directory,
folds it with the two injected aggregators while it is live, renders the two tables with the
injected pure renderers, and publishes each as a keyed markdown artifact — collaborators
injected, so it is tested with fakes and no Prefect server, no S3, no plotting stack.
:func:`materialize_run` is the S3 collaborator: it downloads every cell of every point into the
per-point layout the aggregators read, reusing the cell-object enumeration the redeploy-skip
gate reads (:mod:`slipstream_bench.orchestration.cell_objects`). :func:`render_result_tables` is
the ``@task`` the flow runs once after the loop, binding the live S3 client and run inputs onto
:func:`materialize_run` with :func:`functools.partial` and calling the driver with the real
report-member collaborators.

Only the pure table renderers (:mod:`slipstream_bench.report.chart`) and the aggregators
(:mod:`slipstream_bench.report.aggregation`) are imported here — never the matplotlib plotters,
so this path imports no plotting stack (ADR-0018: the plots ship as a later ticket). The worker
image still carries the plotting stack as a transitive dependency of the report member — see the
orchestration Dockerfile; this claim is import-scope, not image-scope.
"""

import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from functools import partial
from pathlib import Path
from typing import Any

from prefect import task
from prefect.cache_policies import NO_CACHE

from slipstream_bench.contract import SweepGrid, list_engine_points
from slipstream_bench.orchestration.cell_objects import iter_point_cell_objects
from slipstream_bench.report.aggregation import (
    CeilingRow,
    RungRow,
    aggregate_ceilings,
    aggregate_rungs,
)
from slipstream_bench.report.chart import rows_to_markdown, rungs_to_markdown

# The run directory a materialize collaborator opens for the fold: a context manager so the
# downloaded cells live only for the aggregation and are released before the publish.
Materialize = Callable[[], AbstractContextManager[Path]]

# The two aggregators that fold a run directory into ceiling and rung rows, injected so the
# driver stays free of the report member in its own body and is tested with fakes.
CeilingAggregator = Callable[[Path], list[CeilingRow]]
RungAggregator = Callable[[Path], list[RungRow]]

# The two pure table renderers that turn aggregated rows into a markdown table body.
CeilingRenderer = Callable[[list[CeilingRow]], str]
RungRenderer = Callable[[list[RungRow]], str]

# The Prefect markdown-artifact publisher, injected so the driver stays prefect-free and the
# publish path is covered against a fake. In production the task passes
# ``prefect.artifacts.create_markdown_artifact``.
ArtifactPublisher = Callable[..., Any]

# Keyed so Prefect keeps a cross-run history/timeline of each table artifact.
_CEILING_ARTIFACT_KEY = "knob-sweep-ceiling-table"
_CLIFF_ARTIFACT_KEY = "knob-sweep-goodput-cliff"

# The heading each table carries on the parent run page, naming the view the operator reads.
_CEILING_HEADING = "## concurrency ceiling"
_CLIFF_HEADING = "## goodput cliff"


def drive_render_tables(
    *,
    materialize: Materialize,
    aggregate_ceilings: CeilingAggregator,
    aggregate_rungs: RungAggregator,
    render_ceiling_table: CeilingRenderer,
    render_cliff_table: RungRenderer,
    publish: ArtifactPublisher,
) -> None:
    """Fold the materialized run and publish its ceiling and cliff tables as artifacts.

    The transport-free render core: it opens the materialized run directory, folds it with
    both aggregators while the directory is live, then — after the directory is released —
    renders each fold as a markdown table and publishes it under its own key, ceiling first.
    Any raise (a materialize, aggregate, or publish failure) propagates, so the render task
    fails loud and a re-run re-renders off S3. The two publishes are sequential, not atomic: a
    cliff-publish failure can leave the ceiling artifact published and the cliff absent, but the
    artifacts are keyed, so a re-run overwrites both and restores the pair.

    :param materialize: opens the run directory the aggregators fold, as a context manager
        so the downloaded cells are released once both folds are read.
    :param aggregate_ceilings: folds the run directory into ceiling rows, one per
        point-and-share.
    :param aggregate_rungs: unfolds the run directory into per-rung rows, the goodput cliff.
    :param render_ceiling_table: renders the ceiling rows as a markdown table body.
    :param render_cliff_table: renders the rung rows as a markdown table body.
    :param publish: the markdown-artifact publisher (create_markdown_artifact in production),
        called with ``key`` and ``markdown`` per table.
    """
    with materialize() as run_dir:
        ceilings = aggregate_ceilings(run_dir)
        rungs = aggregate_rungs(run_dir)
    publish(
        key=_CEILING_ARTIFACT_KEY,
        markdown=f"{_CEILING_HEADING}\n\n{render_ceiling_table(ceilings)}\n",
    )
    publish(
        key=_CLIFF_ARTIFACT_KEY,
        markdown=f"{_CLIFF_HEADING}\n\n{render_cliff_table(rungs)}\n",
    )


@contextmanager
def materialize_run(
    *,
    grid: SweepGrid,
    run_prefix: str,
    bucket: str,
    s3_client: Any,
    model: str,
) -> Iterator[Path]:
    """Download a knob-sweep run's whole cell set from S3 into the layout the aggregators read.

    Enumerates every engine point of the grid and, per point, downloads each of its cells
    into ``<run_dir>/<point-slug>/<basename>`` — the per-point subdir layout
    :func:`slipstream_bench.report.aggregation.aggregate_ceilings` folds. The run is
    materialized into a temporary directory yielded to the caller and removed on exit, so the
    cells live only for the fold. An absent object raises rather than degrading to a partial
    run: the render runs after the sweep persisted every cell, so a missing one is a real
    failure a re-run re-reads off S3, not a pending cell (contrast the redeploy-skip gate).

    The cell addressing is commercial-independent — a cell keys by its ``pshare/burst/mc``
    coordinate whichever arm produced it — so the run materializes without the sweep's
    commercial flag, sidestepping the tokenizer guard that only gates building a *new*
    commercial sweep, not reading a finished run's results.

    :param grid: the validated grid the run's points and cells are enumerated from.
    :param run_prefix: the knob sweep's shared run id (cells nest under
        ``<run_prefix>/<point-slug>``).
    :param bucket: the results bucket the cell objects live in.
    :param s3_client: the boto3 S3 client (or stand-in) objects are downloaded with.
    :param model: the served model id (folded into each point's sweep config).
    :return: the populated run directory, live for the duration of the ``with`` block.
    :raise ClientError: on any S3 download failure, including an absent object — the render
        fails loud and a re-run re-reads off S3 rather than folding a partial run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        for point in list_engine_points(grid):
            point_dir = run_dir / point.slug()
            point_dir.mkdir()
            for key, basename in iter_point_cell_objects(
                point, grid=grid, run_prefix=run_prefix, model=model
            ):
                s3_client.download_file(bucket, key, str(point_dir / basename))
        yield run_dir


@task(name="render-result-tables", cache_policy=NO_CACHE)
def render_result_tables(
    *,
    grid: SweepGrid,
    run_prefix: str,
    bucket: str,
    s3_client: Any,
    model: str,
    publish: ArtifactPublisher,
) -> None:
    """Render the run's ceiling and cliff tables to the parent run page — the terminal task.

    The ``@task`` the knob-sweep flow runs once after its point loop: it binds the live S3
    client and run inputs onto :func:`materialize_run` with :func:`functools.partial` and drives
    the render with the report member's real aggregators and pure table renderers. One task, not split
    per table: both folds come from the one materialized run, so splitting would re-download
    it. A failure (materialize or publish) is isolated to this task — the sweep's cells are
    already persisted — and a re-run re-renders idempotently off S3. The task carries no retry
    budget, so recovery is a re-run, not an automatic Prefect retry.

    Caching is off (``cache_policy=NO_CACHE``): the task takes live, unhashable handles — the
    boto3 S3 client (an SSLContext) and the publish function — which the default inputs-hashing
    policy cannot serialize into a key, so it would fail to hash on every run. The render is
    terminal and idempotent off S3, so there is nothing to cache regardless.

    :param grid: the validated grid the run's points and cells are enumerated from.
    :param run_prefix: the knob sweep's shared run id the cells nest under.
    :param bucket: the results bucket the cell objects live in.
    :param s3_client: the boto3 S3 client the cells are materialized with.
    :param model: the served model id (folded into each point's sweep config).
    :param publish: the markdown-artifact publisher (create_markdown_artifact in production).
    """

    drive_render_tables(
        materialize=partial(
            materialize_run,
            grid=grid,
            run_prefix=run_prefix,
            bucket=bucket,
            s3_client=s3_client,
            model=model,
        ),
        aggregate_ceilings=aggregate_ceilings,
        aggregate_rungs=aggregate_rungs,
        render_ceiling_table=rows_to_markdown,
        render_cliff_table=rungs_to_markdown,
        publish=publish,
    )
