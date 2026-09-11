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


def test_no_args_shows_help() -> None:
    """Invoking the app with no subcommand renders help rather than erroring blankly."""
    result = runner.invoke(app, [])

    assert "serve-sweep" in result.output
    assert "cost" in result.output
    assert "prefix-cache" in result.output


def test_each_unimplemented_subcommand_fails_loudly() -> None:
    """Every stub subcommand exits non-zero with a not-implemented notice."""
    for command in ("serve-sweep", "cost", "prefix-cache"):
        result = runner.invoke(app, [command])
        assert result.exit_code == 1, f"{command} exited {result.exit_code}"
        assert f"{command} is not implemented yet" in result.output
