"""The knob sweep's terminal result render: fold the whole run and publish its tables and plots.

Exercises the transport-free render core (drive_render) against injected fakes — no
Prefect server, no S3, no plotting stack — mirroring tests/orchestration/test_knob_sweep.py:
the materialized run directory is folded by both aggregators while it is live, the two pure
renderers turn the rows into markdown, the two plotters draw them to PNG bytes, and each table
and plot is published as its own keyed artifact, tables-then-plots in ceiling-then-cliff order.
A publish or upload failure propagates, so the render task fails loud and a re-run re-renders
off S3. The S3 materialize collaborator (materialize_run) is exercised against a fake S3 that
serves each cell object: it downloads every point's cells into the per-point layout the
aggregators read, and a missing cell aborts the render rather than aggregating a partial run.
"""

import base64
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
    drive_render,
    materialize_run,
    publish_data_uri_plot,
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
    images: list[tuple[str, bytes, str, str, str]]


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
    the plot named by ``store_raises_name`` (so a failure can be placed after the ceiling plot
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
            raise RuntimeError("plot render failed")
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

    images: list[tuple[str, bytes, str, str, str]] = []

    def publish_plot(
        *, key: str, data: bytes, s3_key: str, heading: str, blurb: str
    ) -> None:
        events.append(f"publish-image:{key}")
        images.append((key, data, s3_key, heading, blurb))

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
        materialize=fakes.materialize,
        aggregate_ceilings=fakes.aggregate_ceilings,
        aggregate_rungs=fakes.aggregate_rungs,
        render_ceiling_table=fakes.render_ceiling_table,
        render_cliff_table=fakes.render_cliff_table,
        publish=fakes.publish,
        render_ceiling_plot=fakes.render_ceiling_plot,
        render_cliff_plot=fakes.render_cliff_plot,
        store_plot=fakes.store_plot,
        publish_plot=fakes.publish_plot,
    )


def test_folds_the_whole_run_then_publishes_tables_then_plots(tmp_path: Path) -> None:
    events: list[str] = []
    fakes = _fakes(events, tmp_path)

    _drive(fakes)

    # Both aggregators fold the run inside the materialized context; after it is released the
    # two tables publish (ceiling first), then each plot is drawn off the same rows, uploaded
    # to S3 as its durable copy, and embedded inline on the run page — ceiling plot first.
    assert events == [
        "materialize:enter",
        "aggregate_ceilings",
        "aggregate_rungs",
        "materialize:exit",
        "publish:knob-sweep-ceiling-table",
        "publish:knob-sweep-goodput-cliff",
        "render-plot:ceiling",
        "store:ceiling-by-max-num-seqs.png",
        "publish-image:knob-sweep-ceiling-plot",
        "render-plot:cliff",
        "store:goodput-by-max-concurrency.png",
        "publish-image:knob-sweep-goodput-cliff-plot",
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
        ("ceiling-by-max-num-seqs.png", b"CEILING-PNG"),
        ("goodput-by-max-concurrency.png", b"CLIFF-PNG"),
    ]
    # Each plot carries its own heading and the blurb its table answers, so an operator reads
    # the drawn shape under the same framing as the numbers.
    assert fakes.images == [
        (
            "knob-sweep-ceiling-plot",
            b"CEILING-PNG",
            "sweeps/run1/charts/ceiling-by-max-num-seqs.png",
            "## concurrency ceiling — plot",
            (
                "The highest offered `--max-concurrency` each engine config held within the "
                "goodput SLO, one row per engine point and prefix-share."
            ),
        ),
        (
            "knob-sweep-goodput-cliff-plot",
            b"CLIFF-PNG",
            "sweeps/run1/charts/goodput-by-max-concurrency.png",
            "## goodput cliff — plot",
            (
                "The goodput fraction at every offered `--max-concurrency`, the per-rung curve "
                "each ceiling is read off (ADR-0009)."
            ),
        ),
    ]


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
    # isolated to the plots, and a re-run re-renders the pair off S3.
    with pytest.raises(RuntimeError, match="s3 upload failed"):
        _drive(fakes)

    assert [key for key, _ in fakes.published] == [
        "knob-sweep-ceiling-table",
        "knob-sweep-goodput-cliff",
    ]
    assert events[-1] == "store:ceiling-by-max-num-seqs.png"
    assert fakes.images == []


def test_a_cliff_plot_failure_leaves_the_ceiling_plot_published(tmp_path: Path) -> None:
    events: list[str] = []
    fakes = _fakes(events, tmp_path, store_raises_name="goodput-by-max-concurrency.png")

    # The publishes are sequential, not atomic: the cliff plot's upload fails only after the
    # ceiling table, cliff table, and ceiling plot are already on the run page. The failure
    # propagates (the render fails loud), leaving that partial state behind — the exact run a
    # re-render recovers by overwriting the keyed artifacts off S3.
    with pytest.raises(RuntimeError, match="s3 upload failed"):
        _drive(fakes)

    assert [key for key, _ in fakes.published] == [
        "knob-sweep-ceiling-table",
        "knob-sweep-goodput-cliff",
    ]
    assert fakes.stored == [("ceiling-by-max-num-seqs.png", b"CEILING-PNG")]
    assert [key for key, *_ in fakes.images] == ["knob-sweep-ceiling-plot"]
    assert events[-1] == "store:goodput-by-max-concurrency.png"


def test_a_plot_render_failure_leaves_the_tables_published(tmp_path: Path) -> None:
    events: list[str] = []
    fakes = _fakes(events, tmp_path, ceiling_plot_raises=True)

    # A plot-render failure propagates like an upload failure, and lands after both tables
    # publish, so the table artifacts are already on the run page and no plot is uploaded or
    # embedded — the drawing failure is isolated to the plots.
    with pytest.raises(RuntimeError, match="plot render failed"):
        _drive(fakes)

    assert [key for key, _ in fakes.published] == [
        "knob-sweep-ceiling-table",
        "knob-sweep-goodput-cliff",
    ]
    assert fakes.stored == []
    assert fakes.images == []
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


# --- publish_data_uri_plot: the inline preview -------------------------------------------


def test_publish_data_uri_plot_embeds_the_png_as_a_base64_data_uri() -> None:
    published: list[tuple[str, str]] = []

    def publish(*, key: str, markdown: str) -> None:
        published.append((key, markdown))

    publish_data_uri_plot(
        key="knob-sweep-ceiling-plot",
        data=b"CEILING-PNG",
        s3_key="sweeps/run1/charts/ceiling-by-max-num-seqs.png",
        heading="## concurrency ceiling — plot",
        blurb="The ceiling per engine config.",
        publish=publish,
    )

    # The PNG is embedded inline as a base64 data-URI under the heading and blurb, so the plot
    # renders on the run page with no URL to expire; the durable copy is the S3 object.
    encoded = base64.b64encode(b"CEILING-PNG").decode("ascii")
    assert published == [
        (
            "knob-sweep-ceiling-plot",
            (
                "## concurrency ceiling — plot\n\n"
                "The ceiling per engine config.\n\n"
                f"![concurrency ceiling — plot](data:image/png;base64,{encoded})\n"
            ),
        )
    ]
