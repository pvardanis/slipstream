"""Tests for the sweep-grid CLI subcommand: emit grid values to the recipe loop.

Pin the front-end contract the `just knob-sweep` recipe reads through command
substitution: `points` prints the Tier-1 TSV, `ladder` the --max-concurrency rungs,
`burstiness` the pinned scalar, all at exit 0; a missing or invalid grid fails loud
at exit 2 rather than emitting a partial sweep the recipe would run blind.
"""

from pathlib import Path

from typer.testing import CliRunner

from slipstream_bench.cli import app

runner = CliRunner()

VALID_GRID = """\
tier1:
  max_num_seqs: [16, 64]
  kv_cache_dtype: [fp8, fp16]
  prefix_caching:
    "on":
      flag: --enable-prefix-caching
      prefix_share: [10, 50, 90]
    "off":
      flag: --no-enable-prefix-caching
      prefix_share: [0]
tier2:
  max_concurrency: [8, 16, 32]
  burstiness: 1.0
"""


def _write_grid(tmp_path: Path, text: str = VALID_GRID) -> Path:
    path = tmp_path / "grid.yaml"
    path.write_text(text)
    return path


def test_points_prints_the_tier1_tsv(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["sweep-grid", "points", "--grid", str(_write_grid(tmp_path))]
    )
    assert result.exit_code == 0
    rows = result.stdout.splitlines()
    assert len(rows) == 8  # 2 max-num-seqs x 2 kv-dtype x 2 prefix-caching
    assert "mns64_kvfp8_pcon\t64\tfp8\t--enable-prefix-caching\t10,50,90" in rows


def test_ladder_prints_the_max_concurrency_rungs(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["sweep-grid", "ladder", "--grid", str(_write_grid(tmp_path))]
    )
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["8", "16", "32"]


def test_burstiness_prints_the_pinned_scalar(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["sweep-grid", "burstiness", "--grid", str(_write_grid(tmp_path))]
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "1.0"


def test_missing_grid_fails_loud_at_exit_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["sweep-grid", "points", "--grid", str(tmp_path / "absent.yaml")]
    )
    assert result.exit_code == 2
    assert "not found" in result.stderr


def test_invalid_grid_fails_loud_at_exit_2(tmp_path: Path) -> None:
    path = _write_grid(
        tmp_path, VALID_GRID.replace("max_num_seqs: [16, 64]", "max_num_seqs: []")
    )
    result = runner.invoke(app, ["sweep-grid", "points", "--grid", str(path)])
    assert result.exit_code == 2
    assert "max_num_seqs" in result.stderr
