"""The knob sweep's terminal result render: fold the whole run and publish its tables.

Exercises the transport-free render core (drive_render_tables) against injected fakes — no
Prefect server, no S3, no plotting stack — mirroring tests/orchestration/test_knob_sweep.py:
the materialized run directory is folded by both aggregators while it is live, the two pure
renderers turn the rows into markdown, and each table is published as its own keyed artifact,
in ceiling-then-cliff order. A publish failure propagates, so the render task fails loud and a
re-run re-renders off S3. The S3 materialize collaborator (materialize_run) is exercised against a fake
S3 that serves each cell object: it downloads every point's cells into the per-point layout the
aggregators read, and a missing cell aborts the render rather than aggregating a partial run.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError

from slipstream_bench.contract import SweepGrid
from slipstream_bench.orchestration.tasks.render import (
    drive_render_tables,
    materialize_run,
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


# --- drive_render_tables: the transport-free render core ---------------------------------


def _fakes(events: list[str], run_dir: Path, *, publish_raises: bool = False):
    """Build the injected render collaborators over a shared per-call event log.

    ``materialize`` yields ``run_dir`` and logs when it is entered and exited; the two
    aggregators and renderers log their call and assert the run dir is live when they fold;
    ``publish`` logs each ``key`` it is handed, or raises on the first call when
    ``publish_raises`` is set, standing in for a Prefect publish failure.
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

    return (
        materialize,
        aggregate_ceilings_fn,
        aggregate_rungs_fn,
        render_ceiling_table,
        render_cliff_table,
        publish,
        published,
    )


def _drive(materialize, agg_c, agg_r, render_c, render_r, publish) -> None:
    drive_render_tables(
        materialize=materialize,
        aggregate_ceilings=agg_c,
        aggregate_rungs=agg_r,
        render_ceiling_table=render_c,
        render_cliff_table=render_r,
        publish=publish,
    )


def test_folds_the_whole_run_then_publishes_ceiling_then_cliff(tmp_path: Path) -> None:
    events: list[str] = []
    (mat, agg_c, agg_r, render_c, render_r, publish, published) = _fakes(
        events, tmp_path
    )

    _drive(mat, agg_c, agg_r, render_c, render_r, publish)

    # Both aggregators fold the run inside the materialized context, then both tables
    # publish after it is released — ceiling first, cliff second.
    assert events == [
        "materialize:enter",
        "aggregate_ceilings",
        "aggregate_rungs",
        "materialize:exit",
        "publish:knob-sweep-ceiling-table",
        "publish:knob-sweep-goodput-cliff",
    ]
    keys = [key for key, _ in published]
    assert keys == ["knob-sweep-ceiling-table", "knob-sweep-goodput-cliff"]
    # Each table carries its operator-facing heading above the rendered body, blank-line
    # separated and trailing-newline framed — so a heading or framing regression is caught,
    # not just the table body.
    ceiling_markdown = dict(published)["knob-sweep-ceiling-table"]
    cliff_markdown = dict(published)["knob-sweep-goodput-cliff"]
    assert ceiling_markdown == "## concurrency ceiling\n\nCEILING-TABLE\n"
    assert cliff_markdown == "## goodput cliff\n\nCLIFF-TABLE\n"


def test_a_publish_failure_fails_the_render(tmp_path: Path) -> None:
    events: list[str] = []
    (mat, agg_c, agg_r, render_c, render_r, publish, _published) = _fakes(
        events, tmp_path, publish_raises=True
    )

    # A publish failure propagates, so the render task fails loud and a re-run re-renders off
    # S3 — the cells the sweep already persisted are untouched.
    with pytest.raises(RuntimeError, match="prefect publish failed"):
        _drive(mat, agg_c, agg_r, render_c, render_r, publish)

    # The run was still folded and the context released before the failing publish.
    assert "materialize:exit" in events
    assert events[-1] == "publish:knob-sweep-ceiling-table"


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
