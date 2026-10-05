"""Tests for the bench executor CLI surface (``slipstream.bench.cli``).

Pin the image's front-end contract: one app that dispatches the per-cell executor
and its post-processors — load-cell/load-sweep, cost/commercial-cost, prefix-cache,
and zero-leak — and keeps them wired and discoverable via --help.
"""

import json
import re
from pathlib import Path

import yaml
from slipstream.bench.cli import app
from typer.testing import CliRunner

runner = CliRunner()

# Typer colours an option name when a terminal forces colour (CI does), rendering
# ``--config`` as ``-\x1b[0m\x1b[..m-config`` so the reset between the dashes hides
# the literal from a substring check. Strip the escapes before asserting on text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return _ANSI.sub("", output)


_COST_PROVENANCE = {
    "price_per_hour": 2.0,
    "output_input_ratio": 1,
    "weight_checksum": "sha256:deadbeef",
    "vllm_version": "0.6.3",
    "quant_recipe": "awq_marlin+fp8-kv",
}

_COMMERCIAL_PROVENANCE = {
    "input_price_per_1m": 0.5,
    "output_price_per_1m": 1.5,
    "api": "openai",
    "model": "gpt-4o-mini",
    "price_quoted_on": "2026-09-11",
}


def _cost_config(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "cost.yaml"
    path.write_text(yaml.safe_dump({**_COST_PROVENANCE, **overrides}))
    return path


def _commercial_config(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "commercial.yaml"
    path.write_text(yaml.safe_dump({**_COMMERCIAL_PROVENANCE, **overrides}))
    return path


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


def test_help_lists_the_subcommands() -> None:
    """``--help`` advertises the executor and post-processor commands the image runs."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "load-cell" in result.stdout
    assert "load-sweep" in result.stdout
    assert "cost" in result.stdout
    assert "commercial-cost" in result.stdout
    assert "prefix-cache" in result.stdout
    assert "zero-leak" in result.stdout


def test_no_args_shows_help() -> None:
    """Invoking the app with no subcommand renders help rather than erroring blankly."""
    result = runner.invoke(app, [])

    assert "load-cell" in result.output
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


def test_prefix_cache_selects_the_model_series(tmp_path: Path) -> None:
    """The --model selector reaches scrape and picks that series' rate."""
    result = _result_file(tmp_path)
    before = tmp_path / "before.prom"
    before.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        'vllm:prefix_cache_queries{model_name="m"} 1000.0\n'
        'vllm:prefix_cache_hits{model_name="m"} 200.0\n'
        'vllm:prefix_cache_queries{model_name="other"} 500.0\n'
        'vllm:prefix_cache_hits{model_name="other"} 250.0\n'
    )
    after = tmp_path / "after.prom"
    after.write_text(
        "# TYPE vllm:prefix_cache_queries counter\n"
        'vllm:prefix_cache_queries{model_name="m"} 1100.0\n'
        'vllm:prefix_cache_hits{model_name="m"} 210.0\n'
        'vllm:prefix_cache_queries{model_name="other"} 700.0\n'
        'vllm:prefix_cache_hits{model_name="other"} 450.0\n'
    )

    invoked = runner.invoke(
        app,
        [
            "prefix-cache",
            "--cache-state",
            "cold",
            "--model",
            "other",
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
    # The "other" series advanced 200 hits over 200 queries (1.0); the result's own
    # "m" series would read 0.1, so a regression that dropped the selector flips this.
    assert record["prefix_cache_hit_rate"] == 1.0


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
    assert "cache-state" in _plain(invoked.output)


def test_prefix_cache_requires_a_cache_state(tmp_path: Path) -> None:
    """Omitting --cache-state exits 2: the regime is not optional."""
    result, before, after = _prefix_cache_fixture(tmp_path)

    invoked = runner.invoke(
        app,
        [
            "prefix-cache",
            "--metrics-before",
            str(before),
            "--metrics-after",
            str(after),
            "--result",
            str(result),
        ],
    )

    assert invoked.exit_code == 2
    assert "--cache-state" in _plain(invoked.output)


def test_prefix_cache_rejects_a_malformed_result_file(tmp_path: Path) -> None:
    """Valid paths but an unreadable --result hit the ResultError arm: exit 2, stderr."""
    _result, before, after = _prefix_cache_fixture(tmp_path)
    bad_result = tmp_path / "bad.json"
    bad_result.write_text("{not json")

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
            str(bad_result),
        ],
    )

    assert invoked.exit_code == 2
    assert "cannot read" in invoked.output


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
    """The cost command reads provenance from --config and emits priced JSON."""
    result = _result_file(tmp_path)
    config = _cost_config(tmp_path)

    invoked = runner.invoke(app, ["cost", "--config", str(config), str(result)])

    assert invoked.exit_code == 0, invoked.output
    records = json.loads(invoked.stdout)
    assert len(records) == 1
    assert records[0]["cost_per_1m_input_usd"] == 2.0
    assert records[0]["weight_checksum"] == "sha256:deadbeef"


def test_cost_prices_multiple_files_as_an_ordered_array(tmp_path: Path) -> None:
    """Result files stay path args: two files yield an ordered two-element array."""
    first = _result_file(tmp_path)
    second = tmp_path / "other.json"
    second.write_text(first.read_text())
    config = _cost_config(tmp_path)

    invoked = runner.invoke(
        app, ["cost", "--config", str(config), str(first), str(second)]
    )

    assert invoked.exit_code == 0, invoked.output
    records = json.loads(invoked.stdout)
    assert [r["source"] for r in records] == [str(first), str(second)]


def test_cost_rejects_a_malformed_result_file(tmp_path: Path) -> None:
    """A file that exists but is not JSON hits the ResultError arm: exit 2, stderr."""
    result = tmp_path / "bad.json"
    result.write_text("{not json")
    config = _cost_config(tmp_path)

    invoked = runner.invoke(app, ["cost", "--config", str(config), str(result)])

    assert invoked.exit_code == 2
    assert "cannot read" in invoked.output


def test_cost_requires_a_config(tmp_path: Path) -> None:
    """Omitting --config exits 2: the provenance is not optional."""
    result = _result_file(tmp_path)

    invoked = runner.invoke(app, ["cost", str(result)])

    assert invoked.exit_code == 2


def test_cost_rejects_a_non_positive_price(tmp_path: Path) -> None:
    """A zero price in the config exits 2 with a diagnostic, never a $0 figure."""
    result = _result_file(tmp_path)
    config = _cost_config(tmp_path, price_per_hour=0)

    invoked = runner.invoke(app, ["cost", "--config", str(config), str(result)])

    assert invoked.exit_code == 2
    assert "price_per_hour" in invoked.output


def test_commercial_cost_prices_a_result_to_stdout(tmp_path: Path) -> None:
    """The commercial-cost command reads quoted rates from --config and prices."""
    result = _result_file(tmp_path)
    config = _commercial_config(tmp_path)

    invoked = runner.invoke(
        app, ["commercial-cost", "--config", str(config), str(result)]
    )

    assert invoked.exit_code == 0, invoked.output
    records = json.loads(invoked.stdout)
    assert len(records) == 1
    assert records[0]["cost_per_1m_input_usd"] == 0.5
    assert records[0]["cost_per_1m_output_usd"] == 1.5
    assert records[0]["run_cost_usd"] == 0.5
    assert records[0]["api"] == "openai"


def test_commercial_cost_rejects_a_non_positive_rate(tmp_path: Path) -> None:
    """A zero input rate in the config exits 2, never a free-token figure."""
    result = _result_file(tmp_path)
    config = _commercial_config(tmp_path, input_price_per_1m=0)

    invoked = runner.invoke(
        app, ["commercial-cost", "--config", str(config), str(result)]
    )

    assert invoked.exit_code == 2
    assert "input_price_per_1m" in invoked.output


def test_commercial_cost_rejects_a_non_iso_quote_date(tmp_path: Path) -> None:
    """A free-text quote date in the config exits 2 with a diagnostic."""
    result = _result_file(tmp_path)
    config = _commercial_config(tmp_path, price_quoted_on="last tuesday")

    invoked = runner.invoke(
        app, ["commercial-cost", "--config", str(config), str(result)]
    )

    assert invoked.exit_code == 2
    assert "price_quoted_on" in invoked.output


def test_commercial_cost_rejects_a_malformed_result_file(tmp_path: Path) -> None:
    """A file that exists but is not JSON hits the ResultError arm: exit 2, stderr."""
    result = tmp_path / "bad.json"
    result.write_text("{not json")
    config = _commercial_config(tmp_path)

    invoked = runner.invoke(
        app, ["commercial-cost", "--config", str(config), str(result)]
    )

    assert invoked.exit_code == 2
    assert "cannot read" in invoked.output


def _sweep_files(
    tmp_path: Path, instances: dict[str, object], volumes: dict[str, object]
) -> tuple[Path, Path]:
    inst = tmp_path / "instances.json"
    inst.write_text(json.dumps(instances))
    vols = tmp_path / "volumes.json"
    vols.write_text(json.dumps(volumes))
    return inst, vols


def test_zero_leak_clean_teardown_exits_zero(tmp_path: Path) -> None:
    """No tagged leftovers exits 0 — the passing money-safety case."""
    inst, vols = _sweep_files(tmp_path, {"Reservations": []}, {"Volumes": []})

    invoked = runner.invoke(
        app, ["zero-leak", "--instances", str(inst), "--volumes", str(vols)]
    )

    assert invoked.exit_code == 0, invoked.output
    assert "zero" in invoked.stdout.lower()


def test_zero_leak_reports_a_running_instance_and_exits_one(tmp_path: Path) -> None:
    """A surviving g5 exits non-zero and names the instance on stderr."""
    inst, vols = _sweep_files(
        tmp_path,
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-abc",
                            "State": {"Name": "running"},
                            "InstanceType": "g5.xlarge",
                        }
                    ]
                }
            ]
        },
        {"Volumes": []},
    )

    invoked = runner.invoke(
        app, ["zero-leak", "--instances", str(inst), "--volumes", str(vols)]
    )

    assert invoked.exit_code == 1
    assert "i-abc" in invoked.output


def test_zero_leak_reports_a_surviving_volume_and_exits_one(tmp_path: Path) -> None:
    """An orphaned volume exits non-zero and names the volume on stderr."""
    inst, vols = _sweep_files(
        tmp_path,
        {"Reservations": []},
        {"Volumes": [{"VolumeId": "vol-abc", "State": "available", "Size": 100}]},
    )

    invoked = runner.invoke(
        app, ["zero-leak", "--instances", str(inst), "--volumes", str(vols)]
    )

    assert invoked.exit_code == 1
    assert "vol-abc" in invoked.output


def test_zero_leak_malformed_input_exits_two(tmp_path: Path) -> None:
    """Malformed AWS JSON fails loud (exit 2) rather than reading as clean."""
    inst, vols = _sweep_files(tmp_path, {"Reservations": {}}, {"Volumes": []})

    invoked = runner.invoke(
        app, ["zero-leak", "--instances", str(inst), "--volumes", str(vols)]
    )

    assert invoked.exit_code == 2
    assert "Reservations" in invoked.output
