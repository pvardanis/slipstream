"""Tests for the slipstream-bench CLI surface.

Pin the front-end contract: an app that dispatches the three benchmark
subcommands and keeps them wired and discoverable via --help.
"""

from typer.testing import CliRunner

from slipstream_bench.cli import app

runner = CliRunner()


def test_help_lists_the_three_subcommands() -> None:
    """``--help`` advertises serve-sweep, cost, and prefix-cache."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "serve-sweep" in result.stdout
    assert "cost" in result.stdout
    assert "prefix-cache" in result.stdout


def test_each_subcommand_is_invokable() -> None:
    """Every stub subcommand runs and exits cleanly."""
    for command in ("serve-sweep", "cost", "prefix-cache"):
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, f"{command} exited {result.exit_code}"
