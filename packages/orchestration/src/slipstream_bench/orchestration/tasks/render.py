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
and publishes it as an inline image artifact on the run page — collaborators injected, so it is
tested with fakes and no Prefect server, no S3, no plotting-to-disk. :func:`materialize_run` is
the S3 collaborator: it downloads every cell of every point into the per-point layout the
aggregators read, reusing the cell-object enumeration the redeploy-skip gate reads
(:mod:`slipstream_bench.orchestration.cell_objects`). :func:`upload_plot` uploads a plot's PNG
as the run's durable copy; :func:`publish_image_plot` publishes an image artifact pointing at
that PNG's public S3 URL. :func:`render_results` is the ``@task`` the flow runs once after the
loop, binding the live S3 client and run inputs onto the S3 collaborators with
:func:`functools.partial` and calling the driver with the real report-member collaborators.

The pure table renderers (:mod:`slipstream_bench.report.chart`), the aggregators
(:mod:`slipstream_bench.report.aggregation`), and the matplotlib plotters
(:mod:`slipstream_bench.report.plotters`) are imported here: this render is where orchestration
exercises the report member's plotting path and the worker image's matplotlib stack (ADR-0018).
"""

import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
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

# Publishes a plot as an inline image artifact on the run page. Injected so the driver stays
# prefect-free; in production the task binds :func:`publish_image_plot`.
PlotPublisher = Callable[..., None]


@dataclass(frozen=True, kw_only=True)
class RunFold:
    """The collaborators that open the materialized run and fold it into ceiling and rung rows.

    The render's first stage: ``materialize`` opens the run directory as a context manager so
    the downloaded cells live only for the fold, and the two aggregators read that live
    directory into the ceiling and per-rung rows the later stages publish.
    """

    materialize: Materialize
    aggregate_ceilings: CeilingAggregator
    aggregate_rungs: RungAggregator


@dataclass(frozen=True, kw_only=True)
class TablePublish:
    """The collaborators that render the folded rows into the two markdown table artifacts.

    The render's table stage: the two pure renderers turn the ceiling and rung rows into
    markdown bodies, and ``publish`` emits each as its own keyed markdown artifact.
    """

    render_ceiling: CeilingRenderer
    render_cliff: RungRenderer
    publish: ArtifactPublisher


@dataclass(frozen=True, kw_only=True)
class PlotPublish:
    """The collaborators that draw each plot, upload it to S3, and publish it inline.

    The render's plot stage: the two plotters draw the same folded rows to PNG bytes,
    ``store`` uploads each as the run's durable copy and returns its S3 key, and ``publish``
    embeds it as an inline image artifact pointing at that key's public URL.
    """

    render_ceiling: CeilingPlotter
    render_cliff: RungPlotter
    store: PlotStore
    publish: PlotPublisher


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
    fold: RunFold,
    tables: TablePublish,
    plots: PlotPublish,
) -> None:
    """Fold the materialized run and publish its ceiling and cliff tables and plots as artifacts.

    The transport-free render core: it opens the materialized run directory, folds it with both
    aggregators while the directory is live, then — after the directory is released — publishes
    each fold as a markdown table (ceiling first) and then, off the same rows, draws each plot,
    uploads it to S3 as its durable copy, and publishes it as an inline image artifact on the run
    page. The tables publish before the plots, so a plot or upload failure leaves the tables on
    the run page. The cliff plot draws before the ceiling plot: the cliff renders off any run that
    held a rung, while the ceiling plot has nothing to draw when no point reached a ceiling and
    raises — so a no-ceiling run still publishes its diagnostic cliff plot before that raise. Any
    raise (a materialize, aggregate, publish, plot, or upload failure) propagates, so the render
    task fails loud and a re-run re-renders off S3. The publishes are sequential, not atomic: a
    later failure can leave the earlier artifacts published and the rest absent, but the artifacts
    are keyed, so a re-run overwrites and restores the set.

    The collaborators are grouped by render stage: :class:`RunFold` opens and folds the run,
    :class:`TablePublish` renders and publishes the two tables, and :class:`PlotPublish` draws,
    uploads, and embeds the two plots.

    :param fold: opens the run directory and folds it into ceiling and rung rows.
    :param tables: renders the two folds as markdown tables and publishes each as an artifact.
    :param plots: draws each fold to PNG, uploads it as the durable copy, and embeds it inline.
    """
    with fold.materialize() as run_dir:
        ceilings = fold.aggregate_ceilings(run_dir)
        rungs = fold.aggregate_rungs(run_dir)
    tables.publish(
        key=_CEILING_ARTIFACT_KEY,
        markdown=f"{_CEILING_HEADING}\n\n{_CEILING_BLURB}\n\n{tables.render_ceiling(ceilings)}\n",
    )
    tables.publish(
        key=_CLIFF_ARTIFACT_KEY,
        markdown=f"{_CLIFF_HEADING}\n\n{_CLIFF_BLURB}\n\n{tables.render_cliff(rungs)}\n",
    )
    cliff_png = plots.render_cliff(rungs)
    cliff_object = plots.store(name=_CLIFF_PNG_NAME, data=cliff_png)
    plots.publish(
        key=_CLIFF_PLOT_KEY,
        s3_key=cliff_object,
        heading=_CLIFF_PLOT_HEADING,
        blurb=_CLIFF_PLOT_BLURB,
    )
    ceiling_png = plots.render_ceiling(ceilings)
    ceiling_object = plots.store(name=_CEILING_PNG_NAME, data=ceiling_png)
    plots.publish(
        key=_CEILING_PLOT_KEY,
        s3_key=ceiling_object,
        heading=_CEILING_PLOT_HEADING,
        blurb=_CEILING_PLOT_BLURB,
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

    The PNG lands at ``sweeps/<run_prefix>/charts/<name>``, beside the run's cells, under the
    world-readable charts prefix the image artifact points at by public URL (ADR-0018 Amendment).
    ``ContentType`` is ``image/png`` so a browser renders it inline rather than downloading it.

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


def publish_image_plot(
    *,
    key: str,
    s3_key: str,
    heading: str,
    blurb: str,
    bucket: str,
    region: str,
    publish: ArtifactPublisher,
) -> None:
    """Publish a plot as an inline image artifact pointing at its public S3 URL.

    The artifact references the uploaded PNG by its virtual-hosted S3 URL, so the UI renders
    the plot inline and zoomable and the link never expires — the charts prefix is world-readable
    (ADR-0018 Amendment). The heading and blurb ride on the artifact's description, since an image
    artifact carries no markdown body of its own.

    :param key: the plot artifact's key, versioned across runs by Prefect.
    :param s3_key: the plot's S3 object key, the public URL the image artifact points at.
    :param heading: the heading the plot carries on the run page.
    :param blurb: the one-line blurb under the heading.
    :param bucket: the results bucket the public URL addresses.
    :param region: the bucket's region, named in the virtual-hosted URL.
    :param publish: the image-artifact publisher (create_image_artifact in production), called
        with ``image_url``, ``key``, and ``description``.
    """
    image_url = f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"
    publish(image_url=image_url, key=key, description=f"{heading}\n\n{blurb}")


@task(name="render-results", cache_policy=NO_CACHE)
def render_results(
    *,
    grid: SweepGrid,
    run_prefix: str,
    bucket: str,
    region: str,
    s3_client: Any,
    model: str,
    publish: ArtifactPublisher,
    publish_image: ArtifactPublisher,
) -> None:
    """Render the run's ceiling and cliff tables and plots to the parent run page — the terminal task.

    The ``@task`` the knob-sweep flow runs once after its point loop: it binds the live S3
    client and run inputs onto :func:`materialize_run` and :func:`upload_plot` with
    :func:`functools.partial` and drives the render with the report member's real aggregators,
    pure table renderers, and matplotlib plotters, publishing each plot as an image artifact with
    :func:`publish_image_plot` bound to ``publish_image`` and the bucket's public URL. One
    task, not split per artifact: both folds come from the one materialized run, so splitting
    would re-download it. A failure (materialize, publish, plot, or upload) is isolated to this
    task — the sweep's cells are already persisted — and a re-run re-renders idempotently off S3.
    The task carries no retry budget, so recovery is a re-run, not an automatic Prefect retry.

    Caching is off (``cache_policy=NO_CACHE``): the task takes live, unhashable handles — the
    boto3 S3 client (an SSLContext) and the publish function — which the default inputs-hashing
    policy cannot serialize into a key, so it would fail to hash on every run. The render is
    terminal and idempotent off S3, so there is nothing to cache regardless.

    :param grid: the validated grid the run's points and cells are enumerated from.
    :param run_prefix: the knob sweep's shared run id the cells nest under.
    :param bucket: the results bucket the cell objects live in.
    :param region: the bucket's region, named in each plot's public image-artifact URL.
    :param s3_client: the boto3 S3 client the cells are materialized with and the plots uploaded with.
    :param model: the served model id (folded into each point's sweep config).
    :param publish: the markdown-artifact publisher (create_markdown_artifact in production),
        binding the two tables.
    :param publish_image: the image-artifact publisher (create_image_artifact in production),
        binding the two plots to their public S3 URLs.
    """

    drive_render(
        fold=RunFold(
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
        ),
        tables=TablePublish(
            render_ceiling=rows_to_markdown,
            render_cliff=rungs_to_markdown,
            publish=publish,
        ),
        plots=PlotPublish(
            render_ceiling=plot_ceilings_png,
            render_cliff=plot_cliffs_png,
            store=partial(
                upload_plot, run_prefix=run_prefix, bucket=bucket, s3_client=s3_client
            ),
            publish=partial(
                publish_image_plot,
                bucket=bucket,
                region=region,
                publish=publish_image,
            ),
        ),
    )
