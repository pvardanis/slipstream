"""Tests for the slipstream-bench CLI surface.

Pin the front-end contract: an app that dispatches the three benchmark
subcommands and keeps them wired and discoverable via --help.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from slipstream_bench.cli import app

runner = CliRunner()

_PINS = [
    "--weight-checksum",
    "sha256:deadbeef",
    "--vllm-version",
    "0.6.3",
    "--quant-recipe",
    "awq_marlin+fp8-kv",
]


def _result_file(tmp_path: Path) -> Path:
    result = tmp_path / "cell.json"
    result.write_text(
        json.dumps(
            {
                "model_id": "m",
                "duration": 3600.0,
                "completed": 100,
                "total_input_tokens": 1_000_000,
                "total_output_tokens": 0,
            }
        )
    )
    return result


def test_help_lists_the_three_subcommands() -> None:
    """``--help`` advertises serve-sweep, cost, and prefix-cache."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "serve-sweep" in result.stdout
    assert "cost" in result.stdout
    assert "prefix-cache" in result.stdout


def test_no_args_shows_help() -> None:
    """Invoking the app with no subcommand renders help rather than erroring blankly."""
    result = runner.invoke(app, [])

    assert "serve-sweep" in result.output
    assert "cost" in result.output
    assert "prefix-cache" in result.output


def test_prefix_cache_stub_fails_loudly() -> None:
    """The still-stubbed subcommand exits non-zero with a not-implemented notice."""
    result = runner.invoke(app, ["prefix-cache"])
    assert result.exit_code == 1
    assert "prefix-cache is not implemented yet" in result.output


def test_cost_prices_a_result_to_stdout(tmp_path: Path) -> None:
    """The cost command emits a JSON array of priced records to stdout."""
    result = _result_file(tmp_path)

    invoked = runner.invoke(
        app,
        [
            "cost",
            "--price-per-hour",
            "2.0",
            "--output-input-ratio",
            "1",
            *_PINS,
            str(result),
        ],
    )

    assert invoked.exit_code == 0, invoked.output
    records = json.loads(invoked.stdout)
    assert len(records) == 1
    assert records[0]["cost_per_1m_input_usd"] == 2.0


def test_cost_rejects_a_non_positive_price(tmp_path: Path) -> None:
    """A zero price exits 2 with a diagnostic, never a $0 figure."""
    result = _result_file(tmp_path)

    invoked = runner.invoke(
        app,
        [
            "cost",
            "--price-per-hour",
            "0",
            "--output-input-ratio",
            "1",
            *_PINS,
            str(result),
        ],
    )

    assert invoked.exit_code == 2
    assert "price-per-hour" in invoked.output
