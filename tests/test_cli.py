"""Tests for the root slipstream-bench CLI surface.

Pin the reporting front-end that stays behind in the root member: an app that
dispatches the multi-cell aggregation and report commands and keeps them wired and
discoverable via --help. The per-cell executor and its post-processors now live in
the bench member's CLI (tests/bench/test_cli.py).
"""

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from slipstream_bench.cli import app

runner = CliRunner()

# Typer colours an option name when a terminal forces colour (CI does), rendering
# ``--config`` as ``-\x1b[0m\x1b[..m-config`` so the reset between the dashes hides
# the literal from a substring check. Strip the escapes before asserting on text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return _ANSI.sub("", output)


def test_help_lists_the_subcommands() -> None:
    """``--help`` advertises the reporting commands that stay in the root member."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "aggregate-sweep" in result.stdout
    assert "sweep-grid" in result.stdout
    assert "report" in result.stdout
    assert "chart" in result.stdout


def test_no_args_shows_help() -> None:
    """Invoking the app with no subcommand renders help rather than erroring blankly."""
    result = runner.invoke(app, [])

    assert "aggregate-sweep" in result.output
    assert "report" in result.output


def _report_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write the three arms' JSON as the cost/commercial/prefix-cache tools emit."""
    source = "bench/results/prefix-cache/cold_pshare90_burst1.0.json"
    self_hosted = tmp_path / "self_hosted.json"
    self_hosted.write_text(
        json.dumps(
            [
                {
                    "source": source,
                    "cost_per_1m_input_usd": 0.12,
                    "cost_per_1m_output_usd": 0.36,
                }
            ]
        )
    )
    commercial = tmp_path / "commercial.json"
    commercial.write_text(
        json.dumps(
            [
                {
                    "request_rate": 8.0,
                    "prefix_share": 90,
                    "cost_per_1m_input_usd": 0.15,
                    "cost_per_1m_output_usd": 0.60,
                    "api": "openai",
                    "model": "gpt-4o-mini",
                }
            ]
        )
    )
    prefix = tmp_path / "cold_hit_rate.json"
    prefix.write_text(
        json.dumps(
            {
                "source": source,
                "cache_state": "cold",
                "request_rate": 8.0,
                "prefix_share": 90,
                "client_metrics": {
                    "request_goodput": 7.5,
                    "p95_ttft_ms": 850.0,
                    "p99_ttft_ms": 990.0,
                    "p95_tpot_ms": 42.0,
                    "p99_tpot_ms": 48.0,
                },
            }
        )
    )
    return self_hosted, commercial, prefix


def test_report_emits_segmented_json(tmp_path: Path) -> None:
    """The report command joins the three arms and prints the rows as JSON."""
    self_hosted, commercial, prefix = _report_inputs(tmp_path)

    invoked = runner.invoke(
        app,
        [
            "report",
            "--self-hosted-cost",
            str(self_hosted),
            "--commercial-cost",
            str(commercial),
            "--prefix-cache",
            str(prefix),
        ],
    )

    assert invoked.exit_code == 0, invoked.output
    rows = json.loads(invoked.stdout)
    assert rows[0]["cache_state"] == "cold"
    assert rows[0]["self_hosted_usd_per_1m"]["input"] == 0.12
    assert rows[0]["commercial_usd_per_1m"]["output"] == 0.60


def test_report_renders_markdown_on_request(tmp_path: Path) -> None:
    """--format markdown prints a table instead of JSON."""
    self_hosted, commercial, prefix = _report_inputs(tmp_path)

    invoked = runner.invoke(
        app,
        [
            "report",
            "--self-hosted-cost",
            str(self_hosted),
            "--commercial-cost",
            str(commercial),
            "--prefix-cache",
            str(prefix),
            "--format",
            "markdown",
        ],
    )

    assert invoked.exit_code == 0, invoked.output
    assert "| concurrency |" in invoked.stdout
    assert "cold" in invoked.stdout


def test_report_rejects_an_unjoinable_segment(tmp_path: Path) -> None:
    """A spine run with no commercial arm exits 2 with a diagnostic on stderr."""
    self_hosted, _commercial, prefix = _report_inputs(tmp_path)
    empty_commercial = tmp_path / "empty.json"
    empty_commercial.write_text(json.dumps([]))

    invoked = runner.invoke(
        app,
        [
            "report",
            "--self-hosted-cost",
            str(self_hosted),
            "--commercial-cost",
            str(empty_commercial),
            "--prefix-cache",
            str(prefix),
        ],
    )

    assert invoked.exit_code == 2
    assert "commercial" in invoked.output
