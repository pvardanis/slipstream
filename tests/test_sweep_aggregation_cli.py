"""Tests for the aggregate-sweep CLI subcommand: fold a run dir into the table.

Pin the front-end contract: a run directory of point subdirs folds to a JSON
ceiling table on stdout at exit 0, and an empty or unreadable run fails loud at
exit 2 rather than printing an empty table.
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


def test_aggregates_a_run_dir_into_a_json_ceiling_table(tmp_path: Path) -> None:
    """A point with a holding rung folds to one row carrying its measured ceiling."""
    _write_rung(tmp_path / "mns64_kvfp8_pcon", share=50, cap=32, fraction=0.98)

    result = runner.invoke(app, ["aggregate-sweep", "--run-dir", str(tmp_path)])

    assert result.exit_code == 0
    (row,) = json.loads(result.stdout)
    assert row["max_num_seqs"] == 64
    assert row["kv_cache_dtype"] == "fp8"
    assert row["ceiling"] == 32
    assert row["num_preemptions"] is None
    assert row["failures"]["oom"] is None


def test_an_empty_run_dir_fails_at_exit_2(tmp_path: Path) -> None:
    """A run with no point subdirs is rejected, not printed as an empty table."""
    result = runner.invoke(app, ["aggregate-sweep", "--run-dir", str(tmp_path)])

    assert result.exit_code == 2
    assert "no knob-sweep points" in result.stderr


def test_an_unreadable_cell_fails_at_exit_2(tmp_path: Path) -> None:
    """A point subdir holding a non-JSON rung surfaces the reader's error at exit 2,
    not an uncaught traceback — the except tuple catches ResultError too."""
    point_dir = tmp_path / "mns64_kvfp8_pcon"
    point_dir.mkdir()
    (point_dir / "pshare50_burst1.0_mc32.json").write_text("not json")

    result = runner.invoke(app, ["aggregate-sweep", "--run-dir", str(tmp_path)])

    assert result.exit_code == 2
    assert result.stderr.strip()


def test_a_missing_run_dir_is_rejected_by_the_option(tmp_path: Path) -> None:
    """A non-existent run directory fails on the option's own existence check."""
    result = runner.invoke(
        app, ["aggregate-sweep", "--run-dir", str(tmp_path / "absent")]
    )

    assert result.exit_code != 0
