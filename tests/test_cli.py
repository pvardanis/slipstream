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
                "tokenizer_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "duration": 3600.0,
                "completed": 100,
                "total_input_tokens": 1_000_000,
                "total_output_tokens": 0,
            }
        )
    )
    return result


_QUOTE = [
    "--input-price-per-1m",
    "0.5",
    "--output-price-per-1m",
    "1.5",
    "--api",
    "openai",
    "--model",
    "gpt-4o-mini",
    "--price-quoted-on",
    "2026-09-11",
]


def test_help_lists_the_subcommands() -> None:
    """``--help`` advertises serve-sweep, cost, commercial-cost, and prefix-cache."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "serve-sweep" in result.stdout
    assert "cost" in result.stdout
    assert "commercial-cost" in result.stdout
    assert "prefix-cache" in result.stdout
    assert "report" in result.stdout


def test_no_args_shows_help() -> None:
    """Invoking the app with no subcommand renders help rather than erroring blankly."""
    result = runner.invoke(app, [])

    assert "serve-sweep" in result.output
    assert "cost" in result.output
    assert "prefix-cache" in result.output


def _prefix_cache_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    before = tmp_path / "before.prom"
    before.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        'vllm:prefix_cache_queries{model_name="m"} 1000.0\n'
        'vllm:prefix_cache_hits{model_name="m"} 200.0\n'
    )
    after = tmp_path / "after.prom"
    after.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        'vllm:prefix_cache_queries{model_name="m"} 1100.0\n'
        'vllm:prefix_cache_hits{model_name="m"} 210.0\n'
    )
    return _result_file(tmp_path), before, after


def test_prefix_cache_joins_the_delta_rate_to_stdout(tmp_path: Path) -> None:
    """The prefix-cache command emits the joined record as JSON to stdout."""
    result, before, after = _prefix_cache_fixture(tmp_path)

    invoked = runner.invoke(
        app,
        [
            "prefix-cache",
            "--cache-state",
            "cold",
            "--metrics-before",
            str(before),
            "--metrics-after",
            str(after),
            "--result",
            str(result),
        ],
    )

    assert invoked.exit_code == 0, invoked.output
    record = json.loads(invoked.stdout)
    assert record["prefix_cache_hit_rate"] == 0.1
    assert record["cache_state"] == "cold"


def test_prefix_cache_rejects_a_bad_cache_state(tmp_path: Path) -> None:
    """A cache-state outside cold/warm exits 2 with a diagnostic on stderr."""
    result, before, after = _prefix_cache_fixture(tmp_path)

    invoked = runner.invoke(
        app,
        [
            "prefix-cache",
            "--cache-state",
            "lukewarm",
            "--metrics-before",
            str(before),
            "--metrics-after",
            str(after),
            "--result",
            str(result),
        ],
    )

    assert invoked.exit_code == 2
    assert "cache-state" in invoked.output


def test_prefix_cache_rejects_a_missing_snapshot(tmp_path: Path) -> None:
    """A metrics-before path that does not exist is rejected before any parsing."""
    result, _, after = _prefix_cache_fixture(tmp_path)

    invoked = runner.invoke(
        app,
        [
            "prefix-cache",
            "--cache-state",
            "cold",
            "--metrics-before",
            str(tmp_path / "nope.prom"),
            "--metrics-after",
            str(after),
            "--result",
            str(result),
        ],
    )

    assert invoked.exit_code == 2


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


def test_cost_prices_multiple_files_as_an_ordered_array(tmp_path: Path) -> None:
    """Two files yield a two-element array in the order given."""
    first = _result_file(tmp_path)
    second = tmp_path / "other.json"
    second.write_text(first.read_text())

    invoked = runner.invoke(
        app,
        [
            "cost",
            "--price-per-hour",
            "2.0",
            "--output-input-ratio",
            "1",
            *_PINS,
            str(first),
            str(second),
        ],
    )

    assert invoked.exit_code == 0, invoked.output
    records = json.loads(invoked.stdout)
    assert [r["source"] for r in records] == [str(first), str(second)]


def test_cost_rejects_a_malformed_result_file(tmp_path: Path) -> None:
    """A file that exists but is not JSON hits the ResultError arm: exit 2, stderr."""
    result = tmp_path / "bad.json"
    result.write_text("{not json")

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

    assert invoked.exit_code == 2
    assert "cannot read" in invoked.output


def test_commercial_cost_prices_a_result_to_stdout(tmp_path: Path) -> None:
    """The commercial-cost command emits a JSON array priced at the quoted rates."""
    result = _result_file(tmp_path)

    invoked = runner.invoke(app, ["commercial-cost", *_QUOTE, str(result)])

    assert invoked.exit_code == 0, invoked.output
    records = json.loads(invoked.stdout)
    assert len(records) == 1
    assert records[0]["cost_per_1m_input_usd"] == 0.5
    assert records[0]["cost_per_1m_output_usd"] == 1.5
    assert records[0]["run_cost_usd"] == 0.5
    assert records[0]["api"] == "openai"


def test_commercial_cost_rejects_a_non_positive_rate(tmp_path: Path) -> None:
    """A zero input rate exits 2 with a diagnostic, never a free-token figure."""
    result = _result_file(tmp_path)
    quote = ["--input-price-per-1m", "0", *_QUOTE[2:]]

    invoked = runner.invoke(app, ["commercial-cost", *quote, str(result)])

    assert invoked.exit_code == 2
    assert "input-price-per-1m" in invoked.output


def test_commercial_cost_rejects_a_malformed_result_file(tmp_path: Path) -> None:
    """A file that exists but is not JSON hits the ResultError arm: exit 2, stderr."""
    result = tmp_path / "bad.json"
    result.write_text("{not json")

    invoked = runner.invoke(app, ["commercial-cost", *_QUOTE, str(result)])

    assert invoked.exit_code == 2
    assert "cannot read" in invoked.output


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
