"""The knob sweep's terminal result render: fold the whole run and publish its tables and plots.

Exercises the transport-free render core (drive_render) against injected fakes — no
Prefect server, no S3, no plotting stack — mirroring tests/orchestration/test_knob_sweep.py:
the materialized run directory is folded by both aggregators while it is live, the two pure
renderers turn the rows into markdown, the two plotters draw them to PNG bytes, and each table
and plot is published as its own keyed artifact: the tables ceiling-then-cliff, the plots
cliff-then-ceiling so a no-ceiling run still publishes its diagnostic cliff before the ceiling
plot raises on the empty fold. A publish or upload failure propagates, so the render task fails
loud and a re-run re-renders off S3. The S3 materialize collaborator (materialize_run) is
exercised against a fake S3 that serves each cell object: it downloads every point's cells
into the per-point layout the
aggregators read, and a missing cell aborts the render rather than aggregating a partial run.
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError
from prefect.cache_policies import NO_CACHE

from slipstream_bench.contract import SweepGrid
from slipstream_bench.orchestration.tasks.render import (
    PlotPublish,
    RunFold,
    TablePublish,
    drive_render,
    materialize_run,
    publish_image_plot,
    render_results,
    upload_plot,
)
from slipstream_bench.report.aggregation import aggregate_ceilings, aggregate_rungs
from tests.orchestration.cell_object_fakes import ServingCellS3

# A two-arm grid: caching-on over shares [10, 50] and the caching-off share-0 baseline, each
# over a two-rung ladder — four cells per arm, so materialize spans several points and cells.
_GRID = """
tier1:
  max_num_seqs: [64]
  kv_cache_dtype: [fp8]
  prefix_caching:
    "on":
      flag: --enable-prefix-caching
      prefix_share: [10, 50]
    "off":
      flag: --no-enable-prefix-caching
      prefix_share: [0]
tier2:
  max_concurrency: [64, 128]
  burstiness: 1.0
load:
  total_len: 1000
  num_prompts: 500
  num_prefixes: 5
  output_len: 128
  align_blocks: 0
  request_rate: 8
  seed: 0
  goodput: ["ttft:1000", "tpot:50"]
"""

_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"


class _MissingOneS3(ServingCellS3):
    """Serve every key but the one holding ``missing``, which 404s as an absent object."""

    def __init__(self, missing: str) -> None:
        super().__init__()
        self._missing = missing

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        if self._missing in key:
            self.keys.append(key)
            raise ClientError(
                {"Error": {"Code": "404", "Message": "404"}}, "HeadObject"
            )
        super().download_file(_bucket, key, dest)


def _grid() -> SweepGrid:
    return SweepGrid.model_validate(yaml.safe_load(_GRID))


# --- render_results: the terminal @task ---------------------------------------------------


def test_render_task_opts_out_of_caching() -> None:
    # The task takes live, unhashable handles — the boto3 S3 client (an SSLContext) and the
    # publish function — so the default inputs-hashing cache policy cannot compute a key and
    # errors every run. The render is terminal and idempotent off S3 (no retry, a re-run
    # re-renders), so it opts out of caching rather than hashing handles it must not cache.
    assert render_results.cache_policy is NO_CACHE


# --- drive_render: the transport-free render core ----------------------------------------


@dataclass
class _Fakes:
    """The injected render collaborators plus the logs the assertions read."""

    materialize: Any
    aggregate_ceilings: Any
    aggregate_rungs: Any
    render_ceiling_table: Any
    render_cliff_table: Any
    publish: Any
    render_ceiling_plot: Any
    render_cliff_plot: Any
    store_plot: Any
    publish_plot: Any
    published: list[tuple[str, str]]
    stored: list[tuple[str, bytes]]
    images: list[tuple[str, str, str, str]]


def _fakes(
    events: list[str],
    run_dir: Path,
    *,
    publish_raises: bool = False,
    store_raises: bool = False,
    store_raises_name: str | None = None,
    ceiling_plot_raises: bool = False,
) -> _Fakes:
    """Build the injected render collaborators over a shared per-call event log.

    ``materialize`` yields ``run_dir`` and logs when it is entered and exited; the two
    aggregators and table renderers log their call and assert the run dir is live when they
    fold; ``publish`` logs each table ``key`` it is handed, or raises when ``publish_raises``
    is set. The two plot renderers return sentinel PNG bytes off the same rows, the ceiling
    plotter raising when ``ceiling_plot_raises`` is set; ``store_plot`` logs each plot name and
    returns its S3 object key, raising on every call when ``store_raises`` is set or only for
    the plot named by ``store_raises_name`` (so a failure can be placed after the cliff plot
    is already published); ``publish_plot`` logs each plot ``key`` it embeds inline. Each stands
    in for its Prefect or S3 counterpart so the core is exercised with no server, no bucket, and
    no plotting stack.
    """

    @contextmanager
    def materialize():
        events.append("materialize:enter")
        try:
            yield run_dir
        finally:
            events.append("materialize:exit")

    def aggregate_ceilings_fn(folded: Path) -> list[dict[str, Any]]:
        assert folded == run_dir
        events.append("aggregate_ceilings")
        return [{"row": "ceiling"}]

    def aggregate_rungs_fn(folded: Path) -> list[dict[str, Any]]:
        assert folded == run_dir
        events.append("aggregate_rungs")
        return [{"row": "rung"}]

    def render_ceiling_table(rows: list[dict[str, Any]]) -> str:
        assert rows == [{"row": "ceiling"}]
        return "CEILING-TABLE"

    def render_cliff_table(rows: list[dict[str, Any]]) -> str:
        assert rows == [{"row": "rung"}]
        return "CLIFF-TABLE"

    published: list[tuple[str, str]] = []

    def publish(*, key: str, markdown: str) -> None:
        events.append(f"publish:{key}")
        if publish_raises:
            raise RuntimeError("prefect publish failed")
        published.append((key, markdown))

    def render_ceiling_plot(rows: list[dict[str, Any]]) -> bytes:
        assert rows == [{"row": "ceiling"}]
        events.append("render-plot:ceiling")
        if ceiling_plot_raises:
            # The real plot_ceilings_png raises ValueError on a no-ceiling run; match that
            # type and message so this fake exercises the contract drive_render propagates.
            raise ValueError(
                "cannot chart an empty ceiling table: no point held a ceiling within the SLO"
            )
        return b"CEILING-PNG"

    def render_cliff_plot(rows: list[dict[str, Any]]) -> bytes:
        assert rows == [{"row": "rung"}]
        events.append("render-plot:cliff")
        return b"CLIFF-PNG"

    stored: list[tuple[str, bytes]] = []

    def store_plot(*, name: str, data: bytes) -> str:
        events.append(f"store:{name}")
        if store_raises or name == store_raises_name:
            raise RuntimeError("s3 upload failed")
        stored.append((name, data))
        return f"sweeps/run1/charts/{name}"

    images: list[tuple[str, str, str, str]] = []

    def publish_plot(*, key: str, s3_key: str, heading: str, blurb: str) -> None:
        events.append(f"publish-image:{key}")
        images.append((key, s3_key, heading, blurb))

    return _Fakes(
        materialize=materialize,
        aggregate_ceilings=aggregate_ceilings_fn,
        aggregate_rungs=aggregate_rungs_fn,
        render_ceiling_table=render_ceiling_table,
        render_cliff_table=render_cliff_table,
        publish=publish,
        render_ceiling_plot=render_ceiling_plot,
        render_cliff_plot=render_cliff_plot,
        store_plot=store_plot,
        publish_plot=publish_plot,
        published=published,
        stored=stored,
        images=images,
    )


def _drive(fakes: _Fakes) -> None:
    drive_render(
        fold=RunFold(
            materialize=fakes.materialize,
            aggregate_ceilings=fakes.aggregate_ceilings,
            aggregate_rungs=fakes.aggregate_rungs,
        ),
        tables=TablePublish(
            render_ceiling=fakes.render_ceiling_table,
            render_cliff=fakes.render_cliff_table,
            publish=fakes.publish,
        ),
        plots=PlotPublish(
            render_ceiling=fakes.render_ceiling_plot,
            render_cliff=fakes.render_cliff_plot,
            store=fakes.store_plot,
            publish=fakes.publish_plot,
        ),
    )


def test_folds_the_whole_run_then_publishes_tables_then_plots(tmp_path: Path) -> None:
    events: list[str] = []
    fakes = _fakes(events, tmp_path)

    _drive(fakes)

    # Both aggregators fold the run inside the materialized context; after it is released the
    # two tables publish (ceiling first), then each plot is drawn off the same rows, uploaded
    # to S3 as its durable copy, and embedded inline on the run page. The cliff plot draws
    # first: it renders off any run that held a rung, so a run where no point reached a ceiling
    # still publishes its diagnostic cliff before the ceiling plot — the plot that has nothing
    # to draw on a no-ceiling run — is attempted.
    assert events == [
        "materialize:enter",
        "aggregate_ceilings",
        "aggregate_rungs",
        "materialize:exit",
        "publish:knob-sweep-ceiling-table",
        "publish:knob-sweep-goodput-cliff",
        "render-plot:cliff",
        "store:goodput-by-max-concurrency.png",
        "publish-image:knob-sweep-goodput-cliff-plot",
        "render-plot:ceiling",
        "store:ceiling-by-max-num-seqs.png",
        "publish-image:knob-sweep-ceiling-plot",
    ]
    keys = [key for key, _ in fakes.published]
    assert keys == ["knob-sweep-ceiling-table", "knob-sweep-goodput-cliff"]
    # Each table carries its operator-facing heading and a one-line blurb above the rendered
    # body, blank-line separated and trailing-newline framed — so a heading, blurb, or framing
    # regression is caught, not just the table body.
    ceiling_markdown = dict(fakes.published)["knob-sweep-ceiling-table"]
    cliff_markdown = dict(fakes.published)["knob-sweep-goodput-cliff"]
    assert ceiling_markdown == (
        "## concurrency ceiling\n\n"
        "The highest offered `--max-concurrency` each engine config held within the goodput "
        "SLO, one row per engine point and prefix-share.\n\n"
        "CEILING-TABLE\n"
    )
    assert cliff_markdown == (
        "## goodput cliff\n\n"
        "The goodput fraction at every offered `--max-concurrency`, the per-rung curve each "
        "ceiling is read off (ADR-0009).\n\n"
        "CLIFF-TABLE\n"
    )
    # Each plot's rendered bytes are uploaded as the durable copy and handed to the inline
    # publisher under its own S3 object key.
    assert fakes.stored == [
        ("goodput-by-max-concurrency.png", b"CLIFF-PNG"),
        ("ceiling-by-max-num-seqs.png", b"CEILING-PNG"),
    ]
    # Each plot carries its own heading and a blurb reading the drawn shape — its facets, series,
    # and axes — so an operator knows how to read the chart, not just the table it mirrors.
    assert fakes.images == [
        (
            "knob-sweep-goodput-cliff-plot",
            "sweeps/run1/charts/goodput-by-max-concurrency.png",
            "## goodput cliff — plot",
            (
                "Each facet an engine point, a line per prefix-share: goodput fraction against "
                "offered `--max-concurrency` (log-2), the 95% floor the dashed reference line."
            ),
        ),
        (
            "knob-sweep-ceiling-plot",
            "sweeps/run1/charts/ceiling-by-max-num-seqs.png",
            "## concurrency ceiling — plot",
            (
                "Each facet a caching/prefix-share condition, a line per `kv_cache_dtype`: the "
                "sustained `--max-concurrency` ceiling against `max_num_seqs`."
            ),
        ),
    ]


def test_drive_render_logs_a_start_and_a_published_milestone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The terminal render task's two progress milestones an operator reads off its own task run
    # page: a start line as the fold begins and a published line once both tables and plots are
    # on the run page (ADR-0020). Without them the task shows no logs while it works.
    fakes = _fakes([], tmp_path)

    with caplog.at_level(
        logging.INFO, logger="slipstream_bench.orchestration.tasks.render"
    ):
        _drive(fakes)

    messages = [record.getMessage() for record in caplog.records]
    assert any("render" in m and "starting" in m for m in messages)
    assert any("render" in m and "published" in m for m in messages)


def test_a_table_publish_failure_fails_the_render(tmp_path: Path) -> None:
    events: list[str] = []
    fakes = _fakes(events, tmp_path, publish_raises=True)

    # A publish failure propagates, so the render task fails loud and a re-run re-renders off
    # S3 — the cells the sweep already persisted are untouched.
    with pytest.raises(RuntimeError, match="prefect publish failed"):
        _drive(fakes)

    # The run was still folded and the context released before the failing publish.
    assert "materialize:exit" in events
    assert events[-1] == "publish:knob-sweep-ceiling-table"


def test_a_plot_upload_failure_leaves_the_tables_published(tmp_path: Path) -> None:
    events: list[str] = []
    fakes = _fakes(events, tmp_path, store_raises=True)

    # A plot upload failure propagates and fails the render, but it lands after both tables
    # publish, so the #233 table artifacts are already on the run page — the plot failure is
    # isolated to the plots, and a re-run re-renders the pair off S3. The cliff plot uploads
    # first, so its store is the one that fails here.
    with pytest.raises(RuntimeError, match="s3 upload failed"):
        _drive(fakes)

    assert [key for key, _ in fakes.published] == [
        "knob-sweep-ceiling-table",
        "knob-sweep-goodput-cliff",
    ]
    assert events[-1] == "store:goodput-by-max-concurrency.png"
    assert fakes.images == []


def test_a_ceiling_plot_upload_failure_leaves_the_cliff_plot_published(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    fakes = _fakes(events, tmp_path, store_raises_name="ceiling-by-max-num-seqs.png")

    # The publishes are sequential, not atomic: the ceiling plot's upload fails only after the
    # ceiling table, cliff table, and cliff plot are already on the run page. The failure
    # propagates (the render fails loud), leaving that partial state behind — the exact run a
    # re-render recovers by overwriting the keyed artifacts off S3.
    with pytest.raises(RuntimeError, match="s3 upload failed"):
        _drive(fakes)

    assert [key for key, _ in fakes.published] == [
        "knob-sweep-ceiling-table",
        "knob-sweep-goodput-cliff",
    ]
    assert fakes.stored == [("goodput-by-max-concurrency.png", b"CLIFF-PNG")]
    assert [key for key, *_ in fakes.images] == ["knob-sweep-goodput-cliff-plot"]
    assert events[-1] == "store:ceiling-by-max-num-seqs.png"


def test_a_ceiling_plot_render_failure_leaves_the_tables_and_cliff_plot_published(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    fakes = _fakes(events, tmp_path, ceiling_plot_raises=True)

    # In production the ceiling plotter raises on a no-ceiling run; the fake is forced to
    # raise the same ValueError here. The ceiling plot draws last, after both tables and the
    # whole cliff plot are already on the run page — so a no-ceiling run keeps its diagnostic
    # cliff plot and the raise is isolated to the ceiling plot, which a re-render recovers off
    # S3 once the run holds a ceiling.
    with pytest.raises(ValueError, match="no point held a ceiling"):
        _drive(fakes)

    assert [key for key, _ in fakes.published] == [
        "knob-sweep-ceiling-table",
        "knob-sweep-goodput-cliff",
    ]
    assert fakes.stored == [("goodput-by-max-concurrency.png", b"CLIFF-PNG")]
    assert [key for key, *_ in fakes.images] == ["knob-sweep-goodput-cliff-plot"]
    assert events[-1] == "render-plot:ceiling"


# --- materialize_run: the S3 materialize collaborator ------------------------------------


def _materialize_keys(s3: Any) -> list[str]:
    with materialize_run(
        grid=_grid(),
        run_prefix="run1",
        bucket="bench-bucket",
        s3_client=s3,
        model=_MODEL,
    ) as run_dir:
        # Downloaded while the context is live; the aggregators read this very directory.
        ceilings = aggregate_ceilings(run_dir)
        rungs = aggregate_rungs(run_dir)
    assert ceilings and rungs
    return s3.keys


def test_materialize_downloads_every_cell_into_the_per_point_layout() -> None:
    s3 = ServingCellS3()

    keys = _materialize_keys(s3)

    # Two arms: caching-on over shares [10, 50] and the caching-off share-0 baseline, each
    # over a two-rung ladder — six cells, all under the run's per-point prefixes.
    assert len(keys) == 6
    assert sorted(keys) == sorted(
        [
            "sweeps/run1/mns64_kvfp8_pcon/pshare10_burst1.0_mc64.json",
            "sweeps/run1/mns64_kvfp8_pcon/pshare10_burst1.0_mc128.json",
            "sweeps/run1/mns64_kvfp8_pcon/pshare50_burst1.0_mc64.json",
            "sweeps/run1/mns64_kvfp8_pcon/pshare50_burst1.0_mc128.json",
            "sweeps/run1/mns64_kvfp8_pcoff/pshare0_burst1.0_mc64.json",
            "sweeps/run1/mns64_kvfp8_pcoff/pshare0_burst1.0_mc128.json",
        ]
    )


def test_materialize_folds_into_the_grids_ceilings() -> None:
    s3 = ServingCellS3()

    with materialize_run(
        grid=_grid(),
        run_prefix="run1",
        bucket="bench-bucket",
        s3_client=s3,
        model=_MODEL,
    ) as run_dir:
        ceilings = aggregate_ceilings(run_dir)

    # Every rung passed the SLO, so each point-and-share ceiling is the top ladder rung
    # (128), one row per arm-and-share: caching-off share 0, caching-on shares 10 and 50.
    shares = [(row["prefix_caching"], row["prefix_share"]) for row in ceilings]
    assert shares == [(False, 0), (True, 10), (True, 50)]
    assert all(row["ceiling"] == 128 for row in ceilings)


def test_materialize_folds_into_the_grids_rungs() -> None:
    s3 = ServingCellS3()

    with materialize_run(
        grid=_grid(),
        run_prefix="run1",
        bucket="bench-bucket",
        s3_client=s3,
        model=_MODEL,
    ) as run_dir:
        rungs = aggregate_rungs(run_dir)

    # Every cell of every arm-and-share unfolds to its own rung — the goodput cliff behind
    # the ceiling — keyed by (caching, share, offered concurrency), sorted point then share
    # then cap: both rungs of caching-off share 0, then caching-on shares 10 and 50.
    rungs_keyed = [
        (row["prefix_caching"], row["prefix_share"], row["max_concurrency"])
        for row in rungs
    ]
    assert rungs_keyed == [
        (False, 0, 64),
        (False, 0, 128),
        (True, 10, 64),
        (True, 10, 128),
        (True, 50, 64),
        (True, 50, 128),
    ]
    # Every cell served a passing goodput fraction, so no rung fell off the cliff.
    assert all(row["goodput_fraction"] == 1.0 for row in rungs)


def test_materialize_propagates_a_missing_cell() -> None:
    # The render runs after every cell is persisted, so an absent object is a real failure,
    # not a pending cell — it aborts the render, which a re-run re-reads off S3, rather than folding a
    # partial run into a table that reads as complete.
    s3 = _MissingOneS3("pshare50_burst1.0_mc128.json")

    with (
        pytest.raises(ClientError),
        materialize_run(
            grid=_grid(),
            run_prefix="run1",
            bucket="bench-bucket",
            s3_client=s3,
            model=_MODEL,
        ),
    ):
        pass


# --- upload_plot: the durable S3 copy ----------------------------------------------------


class _CapturingS3:
    """Capture each ``put_object`` call so the test reads the key, body, and content type."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> None:
        self.puts.append(
            {"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType}
        )


def test_upload_plot_puts_the_png_under_the_runs_charts_prefix() -> None:
    s3 = _CapturingS3()

    key = upload_plot(
        run_prefix="run1",
        bucket="bench-bucket",
        s3_client=s3,
        name="ceiling-by-max-num-seqs.png",
        data=b"CEILING-PNG",
    )

    # The plot lands beside the run's cells, under its own charts prefix, as the durable copy
    # behind the inline preview — the object key the presigned fallback would presign if it is
    # ever wired onto the publish seam.
    assert key == "sweeps/run1/charts/ceiling-by-max-num-seqs.png"
    assert s3.puts == [
        {
            "Bucket": "bench-bucket",
            "Key": "sweeps/run1/charts/ceiling-by-max-num-seqs.png",
            "Body": b"CEILING-PNG",
            "ContentType": "image/png",
        }
    ]


# --- publish_image_plot: the inline image artifact ---------------------------------------


def test_publish_image_plot_points_an_image_artifact_at_the_public_s3_url() -> None:
    calls: list[dict[str, str]] = []

    def publish(*, image_url: str, key: str, description: str) -> None:
        calls.append({"image_url": image_url, "key": key, "description": description})

    publish_image_plot(
        key="knob-sweep-ceiling-plot",
        s3_key="sweeps/run1/charts/ceiling-by-max-num-seqs.png",
        heading="## concurrency ceiling — plot",
        blurb="The ceiling per engine config.",
        bucket="slipstream-bench-endpoint-results-abc123",
        region="eu-west-1",
        publish=publish,
    )

    # The plot publishes as an image artifact pointing at its public, virtual-hosted S3 URL, so
    # the UI renders it inline and zoomable and the link never expires (the bucket's charts
    # prefix is world-readable). The heading and blurb ride on the artifact's description.
    assert calls == [
        {
            "image_url": (
                "https://slipstream-bench-endpoint-results-abc123.s3.eu-west-1."
                "amazonaws.com/sweeps/run1/charts/ceiling-by-max-num-seqs.png"
            ),
            "key": "knob-sweep-ceiling-plot",
            "description": (
                "## concurrency ceiling — plot\n\nThe ceiling per engine config."
            ),
        }
    ]
