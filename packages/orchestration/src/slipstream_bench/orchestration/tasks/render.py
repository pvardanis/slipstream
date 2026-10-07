"""The knob sweep's terminal result render: fold the whole run and publish its tables and plots (ADR-0018).

After the parent knob-sweep point loop persists every cell to S3, one terminal ``@task``
materializes the whole run from S3, folds it with both aggregators, and publishes the ceiling
and goodput-cliff tables as markdown artifacts and their two plots as inline images on the
parent run page. It runs once, at the end, off the persisted cells — so a materialize, publish,
or plot failure fails only the render, leaving the cells intact, and a re-run re-renders
idempotently off S3 without re-running a cell. The task carries no retry budget, so recovery is
a re-run (manual or a resumed sweep), not an automatic Prefect retry.

Three layers mirror the sweep driver (:mod:`slipstream_bench.orchestration.flows.knob_sweep`).
:func:`drive_render` is the transport-free core: it opens a materialized run directory, folds it
with the two injected aggregators while it is live, publishes the two tables with the injected
pure renderers, then draws each plot off the same rows, uploads it to S3 as its durable copy,
and embeds it inline on the run page — collaborators injected, so it is tested with fakes and no
Prefect server, no S3, no plotting-to-disk. :func:`materialize_run` is the S3 collaborator: it
downloads every cell of every point into the per-point layout the aggregators read, reusing the
cell-object enumeration the redeploy-skip gate reads
(:mod:`slipstream_bench.orchestration.cell_objects`). :func:`upload_plot` uploads a plot's PNG
as the run's durable copy; :func:`publish_data_uri_plot` embeds it inline as a base64 data-URI.
:func:`render_results` is the ``@task`` the flow runs once after the loop, binding the live
S3 client and run inputs onto the S3 collaborators with :func:`functools.partial` and calling the
driver with the real report-member collaborators.

The pure table renderers (:mod:`slipstream_bench.report.chart`), the aggregators
(:mod:`slipstream_bench.report.aggregation`), and the matplotlib plotters
(:mod:`slipstream_bench.report.plotters`) are imported here: this render is where orchestration
exercises the report member's plotting path and the worker image's matplotlib stack (ADR-0018).
"""

import base64
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
from slipstream_bench.report.plotters import plot_ceilings_png, plot_cliffs_png

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

# The two matplotlib plotters that draw a fold straight to PNG bytes, injected so the driver
# stays free of the plotting stack in its own body and is tested with fakes.
CeilingPlotter = Callable[[list[CeilingRow]], bytes]
RungPlotter = Callable[[list[RungRow]], bytes]

# Uploads a plot's PNG to S3 as the run's durable copy and returns its object key. Injected so
# the driver stays S3-free; in production the task binds :func:`upload_plot`.
PlotStore = Callable[..., str]

# Embeds a plot inline on the run page. Injected so the driver stays prefect-free; in production
# the task binds :func:`publish_data_uri_plot`.
PlotPublisher = Callable[..., None]

# Keyed so Prefect keeps a cross-run history/timeline of each table artifact.
_CEILING_ARTIFACT_KEY = "knob-sweep-ceiling-table"
_CLIFF_ARTIFACT_KEY = "knob-sweep-goodput-cliff"

# Keyed so Prefect keeps a cross-run history/timeline of each plot artifact, distinct from its
# table so an operator reads the numbers and the shape as two artifacts.
_CEILING_PLOT_KEY = "knob-sweep-ceiling-plot"
_CLIFF_PLOT_KEY = "knob-sweep-goodput-cliff-plot"

# The object name each plot's PNG uploads under in the run's charts prefix, mirroring the
# filenames :func:`slipstream_bench.report.plotters.write_artifacts` writes to disk.
_CEILING_PNG_NAME = "ceiling-by-max-num-seqs.png"
_CLIFF_PNG_NAME = "goodput-by-max-concurrency.png"

# The heading each table carries on the parent run page, naming the view the operator reads.
_CEILING_HEADING = "## concurrency ceiling"
_CLIFF_HEADING = "## goodput cliff"

# The heading each plot carries, naming the same view the table answers as its drawn shape.
_CEILING_PLOT_HEADING = "## concurrency ceiling — plot"
_CLIFF_PLOT_HEADING = "## goodput cliff — plot"

# A one-line blurb under each heading, so an operator reading the run page knows what the table
# answers without opening an ADR: the ceiling is the headline capacity per config, the cliff the
# per-rung curve it was read off.
_CEILING_BLURB = (
    "The highest offered `--max-concurrency` each engine config held within the goodput "
    "SLO, one row per engine point and prefix-share."
)
_CLIFF_BLURB = (
    "The goodput fraction at every offered `--max-concurrency`, the per-rung curve each "
    "ceiling is read off (ADR-0009)."
)

# Each plot's own blurb reads the drawn shape — its facets, series, and axes — so an operator
# knows how to read the chart under it, not just the table it mirrors.
_CEILING_PLOT_BLURB = (
    "Each facet a caching/prefix-share condition, a line per `kv_cache_dtype`: the "
    "sustained `--max-concurrency` ceiling against `max_num_seqs`."
)
_CLIFF_PLOT_BLURB = (
    "Each facet an engine point, a line per prefix-share: goodput fraction against "
    "offered `--max-concurrency` (log-2), the 95% floor the dashed reference line."
)


def drive_render(
    *,
    materialize: Materialize,
    aggregate_ceilings: CeilingAggregator,
    aggregate_rungs: RungAggregator,
    render_ceiling_table: CeilingRenderer,
    render_cliff_table: RungRenderer,
    publish: ArtifactPublisher,
    render_ceiling_plot: CeilingPlotter,
    render_cliff_plot: RungPlotter,
    store_plot: PlotStore,
    publish_plot: PlotPublisher,
) -> None:
    """Fold the materialized run and publish its ceiling and cliff tables and plots as artifacts.

    The transport-free render core: it opens the materialized run directory, folds it with both
    aggregators while the directory is live, then — after the directory is released — publishes
    each fold as a markdown table (ceiling first) and then, off the same rows, draws each plot,
    uploads it to S3 as its durable copy, and embeds it inline on the run page (ceiling plot
    first). The tables publish before the plots, so a plot or upload failure leaves the tables on
    the run page. Any raise (a materialize, aggregate, publish, plot, or upload failure)
    propagates, so the render task fails loud and a re-run re-renders off S3. The publishes are
    sequential, not atomic: a later failure can leave the earlier artifacts published and the
    rest absent, but the artifacts are keyed, so a re-run overwrites and restores the set.

    :param materialize: opens the run directory the aggregators fold, as a context manager
        so the downloaded cells are released once both folds are read.
    :param aggregate_ceilings: folds the run directory into ceiling rows, one per
        point-and-share.
    :param aggregate_rungs: unfolds the run directory into per-rung rows, the goodput cliff.
    :param render_ceiling_table: renders the ceiling rows as a markdown table body.
    :param render_cliff_table: renders the rung rows as a markdown table body.
    :param publish: the markdown-artifact publisher (create_markdown_artifact in production),
        called with ``key`` and ``markdown`` per table.
    :param render_ceiling_plot: draws the ceiling rows straight to PNG bytes.
    :param render_cliff_plot: draws the rung rows straight to PNG bytes.
    :param store_plot: uploads a plot's PNG as the run's durable copy, called with ``name`` and
        ``data`` and returning the S3 object key.
    :param publish_plot: embeds a plot inline on the run page, called with ``key``, ``data``,
        the plot's ``s3_key``, and its ``heading`` and ``blurb``.
    """
    with materialize() as run_dir:
        ceilings = aggregate_ceilings(run_dir)
        rungs = aggregate_rungs(run_dir)
    publish(
        key=_CEILING_ARTIFACT_KEY,
        markdown=f"{_CEILING_HEADING}\n\n{_CEILING_BLURB}\n\n{render_ceiling_table(ceilings)}\n",
    )
    publish(
        key=_CLIFF_ARTIFACT_KEY,
        markdown=f"{_CLIFF_HEADING}\n\n{_CLIFF_BLURB}\n\n{render_cliff_table(rungs)}\n",
    )
    ceiling_png = render_ceiling_plot(ceilings)
    ceiling_object = store_plot(name=_CEILING_PNG_NAME, data=ceiling_png)
    publish_plot(
        key=_CEILING_PLOT_KEY,
        data=ceiling_png,
        s3_key=ceiling_object,
        heading=_CEILING_PLOT_HEADING,
        blurb=_CEILING_PLOT_BLURB,
    )
    cliff_png = render_cliff_plot(rungs)
    cliff_object = store_plot(name=_CLIFF_PNG_NAME, data=cliff_png)
    publish_plot(
        key=_CLIFF_PLOT_KEY,
        data=cliff_png,
        s3_key=cliff_object,
        heading=_CLIFF_PLOT_HEADING,
        blurb=_CLIFF_PLOT_BLURB,
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


def upload_plot(
    *,
    run_prefix: str,
    bucket: str,
    s3_client: Any,
    name: str,
    data: bytes,
) -> str:
    """Upload a plot's PNG to the run's charts prefix as its durable copy, and return its key.

    The PNG lands at ``sweeps/<run_prefix>/charts/<name>``, beside the run's cells, so the
    durable copy outlives the inline preview on the run page — an expired preview loses the
    inline image, not the plot.

    :param run_prefix: the knob sweep's shared run id the charts nest under.
    :param bucket: the results bucket the plot is uploaded to.
    :param s3_client: the boto3 S3 client (or stand-in) the PNG is put with.
    :param name: the plot's object name within the run's charts prefix.
    :param data: the PNG bytes to upload.
    :return: the object key the PNG was uploaded under.
    :raise ClientError: on any S3 upload failure — the render fails loud, leaving the tables
        already published, and a re-run re-renders off S3.
    """
    key = f"sweeps/{run_prefix}/charts/{name}"
    s3_client.put_object(Bucket=bucket, Key=key, Body=data, ContentType="image/png")
    return key


def publish_data_uri_plot(
    *,
    key: str,
    data: bytes,
    s3_key: str,
    heading: str,
    blurb: str,
    publish: ArtifactPublisher,
) -> None:
    """Embed a plot inline on the run page as a base64 data-URI under its heading and blurb.

    The PNG is embedded inline with no URL, so the plot renders on the run page without a
    presigned link to expire; the durable copy is the S3 object at ``s3_key`` (ADR-0018).

    :param key: the plot artifact's key, versioned across runs by Prefect.
    :param data: the plot's PNG bytes to embed inline.
    :param s3_key: the plot's durable S3 object key — the lasting copy behind the inline
        preview. Carried on the seam so the presigned fallback can swap onto it without a
        signature change; this inline publisher does not read it (ADR-0018).
    :param heading: the heading the plot carries on the run page.
    :param blurb: the one-line blurb under the heading.
    :param publish: the markdown-artifact publisher (create_markdown_artifact in production).
    """
    encoded = base64.b64encode(data).decode("ascii")
    alt = heading.lstrip("#").strip()
    publish(
        key=key,
        markdown=f"{heading}\n\n{blurb}\n\n![{alt}](data:image/png;base64,{encoded})\n",
    )


@task(name="render-results", cache_policy=NO_CACHE)
def render_results(
    *,
    grid: SweepGrid,
    run_prefix: str,
    bucket: str,
    s3_client: Any,
    model: str,
    publish: ArtifactPublisher,
) -> None:
    """Render the run's ceiling and cliff tables and plots to the parent run page — the terminal task.

    The ``@task`` the knob-sweep flow runs once after its point loop: it binds the live S3
    client and run inputs onto :func:`materialize_run` and :func:`upload_plot` with
    :func:`functools.partial` and drives the render with the report member's real aggregators,
    pure table renderers, and matplotlib plotters, embedding each plot inline with
    :func:`publish_data_uri_plot` over the same ``publish`` the tables use. One task, not split
    per artifact: both folds come from the one materialized run, so splitting would re-download
    it. A failure (materialize, publish, plot, or upload) is isolated to this task — the sweep's
    cells are already persisted — and a re-run re-renders idempotently off S3. The task carries
    no retry budget, so recovery is a re-run, not an automatic Prefect retry.

    Caching is off (``cache_policy=NO_CACHE``): the task takes live, unhashable handles — the
    boto3 S3 client (an SSLContext) and the publish function — which the default inputs-hashing
    policy cannot serialize into a key, so it would fail to hash on every run. The render is
    terminal and idempotent off S3, so there is nothing to cache regardless.

    :param grid: the validated grid the run's points and cells are enumerated from.
    :param run_prefix: the knob sweep's shared run id the cells nest under.
    :param bucket: the results bucket the cell objects live in.
    :param s3_client: the boto3 S3 client the cells are materialized with and the plots uploaded with.
    :param model: the served model id (folded into each point's sweep config).
    :param publish: the markdown-artifact publisher (create_markdown_artifact in production).
    """

    drive_render(
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
        render_ceiling_plot=plot_ceilings_png,
        render_cliff_plot=plot_cliffs_png,
        store_plot=partial(
            upload_plot, run_prefix=run_prefix, bucket=bucket, s3_client=s3_client
        ),
        publish_plot=partial(publish_data_uri_plot, publish=publish),
    )
