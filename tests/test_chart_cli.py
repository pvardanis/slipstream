"""Tests for the chart CLI subcommand: fold a run dir into its charts artifacts.

Pin the front-end contract: a run directory of point subdirs writes the ceiling and
cliff tables and both plots under <run_dir>/charts at exit 0, and an empty or unreadable
run fails loud at exit 2 rather than writing an empty chart.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from slipstream_bench.cli import app

runner = CliRunner()


def _write_rung(point_dir: Path, *, share: int, cap: int, fraction: float) -> None:
    point_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "max_concurrency": cap,
        "prefix_share": share,
        "request_goodput": fraction,
        "request_throughput": 1.0,
        "errors": [""],
    }
    (point_dir / f"pshare{share}_burst1.0_mc{cap}.json").write_text(json.dumps(record))


def test_charts_a_run_dir_into_table_and_plot_artifacts(tmp_path: Path) -> None:
    """A run folds to non-empty ceiling and cliff tables and both plots under charts."""
    _write_rung(tmp_path / "mns64_kvfp8_pcon", share=50, cap=32, fraction=0.98)

    result = runner.invoke(app, ["chart", "--run-dir", str(tmp_path)])

    assert result.exit_code == 0
    charts = tmp_path / "charts"
    for name in (
        "ceiling-table.md",
        "ceiling-table.json",
        "ceiling-by-max-num-seqs.png",
        "goodput-cliff.md",
        "goodput-cliff.json",
        "goodput-by-max-concurrency.png",
    ):
        artifact = charts / name
        assert artifact.is_file() and artifact.stat().st_size > 0
        assert str(artifact) in result.stdout


def test_an_empty_run_dir_fails_at_exit_2(tmp_path: Path) -> None:
    """A run with no point subdirs is rejected, not charted as an empty table."""
    result = runner.invoke(app, ["chart", "--run-dir", str(tmp_path)])

    assert result.exit_code == 2
    assert "no knob-sweep points" in result.stderr
    assert not (tmp_path / "charts").exists()


def test_an_unreadable_cell_fails_at_exit_2(tmp_path: Path) -> None:
    """A point subdir holding a non-JSON rung surfaces the reader's error at exit 2."""
    point_dir = tmp_path / "mns64_kvfp8_pcon"
    point_dir.mkdir()
    (point_dir / "pshare50_burst1.0_mc32.json").write_text("not json")

    result = runner.invoke(app, ["chart", "--run-dir", str(tmp_path)])

    assert result.exit_code == 2
    assert "pshare50_burst1.0_mc32.json" in result.stderr


def test_a_missing_run_dir_is_rejected_by_the_option(tmp_path: Path) -> None:
    """A non-existent run directory fails on the option's own existence check."""
    result = runner.invoke(app, ["chart", "--run-dir", str(tmp_path / "absent")])

    assert result.exit_code != 0
