"""The kv-ab flow's terminal render: fold the A/B's records and publish its tables and chart.

After the A/B's arm runs have written every rung's labelled record to S3, one terminal
``@task`` reads those records back off the pointers the flow returns, folds them with the
report member's kv-ab fold, and publishes two markdown tables — each arm's rung as the median
and range across its runs, and each arm run's ceiling — and the three-panel chart, uploaded to
S3 as its durable copy and embedded inline on the flow run page. Reading the flow's own
pointers, rather than listing the run's prefix, folds exactly the rungs this run measured or
resumed. A failure fails only the render: the records are already in S3, so a re-run of the
flow resumes every cached run and re-renders.

:func:`drive_kv_ab_render` is the transport-free core: it calls the report fold, tables, and
plotter directly, and takes only its record reads, publishes, and plot upload injected; :func:`read_kv_ab_record` is the S3 record reader; and
:func:`render_kv_ab_results` is the ``@task`` that binds them to the live S3 client and
Prefect's artifact publishers, reusing the knob sweep's plot upload and image publish
(:mod:`slipstream_bench.orchestration.tasks.render`).
"""

import json
import logging
import urllib.parse
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, get_args

from prefect import task
from prefect.cache_policies import NO_CACHE

from slipstream_bench.contract import SweepAggregationError
from slipstream_bench.contract.kv_ab_record import Arm
from slipstream_bench.orchestration.kv_ab_cell import get_kv_ab_run_prefix
from slipstream_bench.orchestration.tasks.render import (
    ArtifactPublisher,
    PlotPublisher,
    PlotStore,
    publish_image_plot,
    upload_plot,
)
from slipstream_bench.report.kv_ab_aggregation import (
    KvAbRung,
    get_kv_ab_ceilings,
    summarize_kv_ab_rungs,
)
from slipstream_bench.report.kv_ab_chart import (
    kv_ab_ceilings_to_markdown,
    kv_ab_spreads_to_markdown,
)
from slipstream_bench.report.kv_ab_plotters import plot_kv_ab_png

_LOGGER = logging.getLogger(__name__)

# Reads a rung's labelled record off its ``s3://`` pointer. Injected so the driver stays
# S3-free; in production the task binds :func:`read_kv_ab_record`.
RecordReader = Callable[[str], dict[str, object]]

# Keyed so Prefect keeps a cross-run history of each artifact.
_SPREADS_KEY = "kv-ab-rung-spreads"
_CEILINGS_KEY = "kv-ab-ceilings"
_PLOT_KEY = "kv-ab-plot"

# The object name the chart's PNG uploads under in the run's charts prefix.
_PLOT_NAME = "kv-ab-by-max-concurrency.png"

# Each artifact's heading and a one-line blurb naming what it answers.
_SPREADS_HEADING = "## kv-ab rungs"
_SPREADS_BLURB = (
    "Each arm's rung across its runs: the median, and the range in brackets."
)
_CEILINGS_HEADING = "## kv-ab ceilings"
_CEILINGS_BLURB = (
    "The highest offered `--max-concurrency` at which each arm run's goodput fraction "
    "met the 95% floor; an arm's median is blank when any of its runs met it at no rung "
    "or has no record."
)
_PLOT_HEADING = "## kv-ab — plot"
_PLOT_BLURB = (
    "Combined hit rate, p95 TTFT, and goodput fraction against offered "
    "`--max-concurrency` (log-2), a line per arm through its runs' medians, the band "
    "their range, the dashed line the goodput floor."
)


def drive_kv_ab_render(
    *,
    pointers: Sequence[str],
    read_record: RecordReader,
    publish_artifact: ArtifactPublisher,
    store: PlotStore,
    publish_plot: PlotPublisher,
) -> None:
    """Fold the A/B's records and publish its spread and ceiling tables, then its chart.

    The tables publish before the chart, so a plot or upload failure leaves them on the run
    page. Any raise propagates, so the render task fails loud.

    :param pointers: every rung record's ``s3://`` pointer, as the flow returns them.
    :param read_record: reads a record off its pointer.
    :param publish_artifact: the markdown-artifact publisher, called with ``key`` and
        ``markdown``.
    :param store: uploads the chart's PNG and returns its S3 key.
    :param publish_plot: embeds the uploaded chart as an inline image artifact.
    :raise ValueError: when ``pointers`` is empty, so there is nothing to render.
    :raise SweepAggregationError: when a record does not read as a kv-ab rung, or the
        records hold no rungs of an arm.
    """
    if not pointers:
        raise ValueError("cannot render a kv-ab run with no rung records")

    _LOGGER.info("kv-ab render starting: folding %d rung records", len(pointers))
    rungs = [
        KvAbRung.from_record(read_record(pointer), Path(pointer.removeprefix("s3://")))
        for pointer in pointers
    ]
    _reject_missing_arms(rungs)

    spreads = kv_ab_spreads_to_markdown(summarize_kv_ab_rungs(rungs))
    publish_artifact(
        key=_SPREADS_KEY,
        markdown=f"{_SPREADS_HEADING}\n\n{_SPREADS_BLURB}\n\n{spreads}\n",
    )

    ceilings = kv_ab_ceilings_to_markdown(get_kv_ab_ceilings(rungs))
    publish_artifact(
        key=_CEILINGS_KEY,
        markdown=f"{_CEILINGS_HEADING}\n\n{_CEILINGS_BLURB}\n\n{ceilings}\n",
    )

    s3_key = store(name=_PLOT_NAME, data=plot_kv_ab_png(rungs))
    publish_plot(key=_PLOT_KEY, s3_key=s3_key, heading=_PLOT_HEADING, blurb=_PLOT_BLURB)
    _LOGGER.info("kv-ab render published: rung and ceiling tables and the chart")


def read_kv_ab_record(pointer: str, *, s3_client: Any) -> dict[str, object]:
    """Read a rung's labelled record off its ``s3://`` pointer.

    :param pointer: the record's ``s3://<bucket>/<key>`` pointer.
    :param s3_client: the boto3 S3 client (or stand-in) the record is read with.
    :return: the parsed record.
    :raise ValueError: when the pointer is not an ``s3://`` pointer.
    :raise SweepAggregationError: when the object is not valid UTF-8 JSON or not a JSON
        object.
    :raise ClientError: on any S3 read failure, an absent object among them, noted with
        the pointer.
    """
    parts = urllib.parse.urlsplit(pointer)
    if parts.scheme != "s3" or not parts.netloc or not parts.path.strip("/"):
        raise ValueError(f"record pointer {pointer} is not an s3:// pointer")

    try:
        body = s3_client.get_object(Bucket=parts.netloc, Key=parts.path.lstrip("/"))
    except Exception as error:
        # The read error names neither bucket nor key: note the record that failed.
        error.add_note(f"reading kv-ab record {pointer}")
        raise

    try:
        record = json.loads(body["Body"].read())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SweepAggregationError(
            f"record {pointer} is not valid JSON: {error}"
        ) from error

    if not isinstance(record, dict):
        raise SweepAggregationError(f"record {pointer} is not a JSON object")
    return record


@task(name="render-kv-ab-results", cache_policy=NO_CACHE)
def render_kv_ab_results(
    *,
    pointers: Sequence[str],
    run_id: str,
    bucket: str,
    region: str,
    s3_client: Any,
    publish_artifact: ArtifactPublisher,
    publish_image: ArtifactPublisher,
) -> None:
    """Render the A/B's tables and chart to the flow run page — the terminal task.

    The chart uploads under ``sweeps/kv-ab/<run>/charts/``, the world-readable charts
    prefix its image artifact points at (ADR-0018 Amendment). Caching is off: the task takes
    the live S3 client and publish functions, which the default inputs-hashing policy cannot
    hash, and the render is terminal and idempotent off S3 regardless.

    :param pointers: every rung record's ``s3://`` pointer, as the flow returns them.
    :param run_id: the A/B's run id, which its chart nests under.
    :param bucket: the results bucket the records and chart live in.
    :param region: the bucket's region, named in the chart's public image URL.
    :param s3_client: the boto3 S3 client the records are read and the chart uploaded with.
    :param publish_artifact: the markdown-artifact publisher (create_markdown_artifact in
        production).
    :param publish_image: the image-artifact publisher (create_image_artifact in
        production).
    """
    drive_kv_ab_render(
        pointers=pointers,
        read_record=partial(read_kv_ab_record, s3_client=s3_client),
        publish_artifact=publish_artifact,
        store=partial(
            upload_plot,
            run_prefix=get_kv_ab_run_prefix(run_id),
            bucket=bucket,
            s3_client=s3_client,
        ),
        publish_plot=partial(
            publish_image_plot, bucket=bucket, region=region, publish=publish_image
        ),
    )


def _reject_missing_arms(rungs: list[KvAbRung]) -> None:
    """Refuse a run missing an arm's rungs: its chart would compare nothing to nothing."""
    measured = {rung.arm for rung in rungs}
    for arm in get_args(Arm):
        if arm not in measured:
            raise SweepAggregationError(
                f"kv-ab run has no {arm} arm rungs: an A/B compares both arms"
            )
