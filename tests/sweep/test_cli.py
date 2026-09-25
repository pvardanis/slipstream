"""Tests for the load-sweep CLI surface: config-driven dry-run assembly and rejection.

Drives the command-construction seam through ``--config`` + ``--dry-run`` (no vLLM,
no cluster): a config YAML defines the grid, the CLI injects the endpoint, served
model, and out-dir, and a malformed config or a reserved key is rejected with exit 2
before any command is built. The api-key resolver and cell runner are exercised
directly.
"""

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from slipstream_bench.cli import app
from slipstream_bench.sweep.cli import resolve_api_key_env, run_cell
from slipstream_bench.sweep.config import SweepError

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The experiment-defining knobs a config YAML carries; the CLI injects base_url,
# model, and out_dir on top. Mirrors bench/load-sweep.yaml's documented defaults.
_EXPERIMENT: dict[str, object] = {
    "prefix_shares": [10, 50, 90],
    "burstiness_values": [0.2, 1.0],
    "max_concurrency_values": [],
    "total_len": 1000,
    "num_prompts": 500,
    "num_prefixes": 5,
    "output_len": 128,
    "align_blocks": 0,
    "request_rate": 8,
    "seed": 0,
    "goodput": ["ttft:1000", "tpot:50"],
}


def plain(result) -> str:
    """CLI output with terminal styling stripped.

    Typer renders errors through Rich, which wraps option names in ANSI style
    spans when the host forces color (CI runners do). Stripping the styling lets
    a content assertion match the message text on any terminal.
    """
    return _ANSI.sub("", result.output)


def _write_config(tmp_path: Path, **overrides: object) -> Path:
    """Write an experiment config YAML with overrides and return its path."""
    path = tmp_path / "load-sweep.yaml"
    path.write_text(yaml.safe_dump({**_EXPERIMENT, **overrides}))
    return path


def _dry_run(config: Path, *args: str):
    """Invoke load-sweep with the given config, --dry-run, and extra arguments."""
    return runner.invoke(
        app, ["load-sweep", "--config", str(config), "--dry-run", *args]
    )


# --- the committed example config drives a bare dry run ----------------------


def test_default_dry_run_reads_the_committed_config() -> None:
    """A bare dry run reads bench/load-sweep.yaml and emits its 3x2 default grid."""
    result = runner.invoke(app, ["load-sweep", "--dry-run"])

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("vllm bench serve") == 6
    assert result.stdout.count("--goodput ttft:1000 tpot:50") == 6
    assert result.stdout.count("--seed 0") == 6
    assert "--model Qwen/Qwen2.5-0.5B-Instruct" in result.stdout
    assert "--prefix-repetition-prefix-len 100" in result.stdout
    assert "--prefix-repetition-prefix-len 500" in result.stdout
    assert "--prefix-repetition-prefix-len 900" in result.stdout
    assert result.stdout.count("--burstiness 0.2") == 3
    assert result.stdout.count("--burstiness 1.0") == 3


# --- the config defines the grid, the CLI injects the context ----------------


def test_config_defines_the_grid_and_split(tmp_path: Path) -> None:
    """The config's shares and total_len define the grid and each cell's split."""
    config = _write_config(tmp_path, prefix_shares=[25, 50, 90])
    result = _dry_run(config)

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("vllm bench serve") == 6
    assert "--prefix-repetition-prefix-len 250" in result.stdout
    assert "--prefix-repetition-suffix-len 750" in result.stdout


def test_config_ladders_max_concurrency_closed_loop(tmp_path: Path) -> None:
    """A max-concurrency ladder multiplies the grid and caps in-flight per rung."""
    config = _write_config(
        tmp_path,
        prefix_shares=[90],
        burstiness_values=[1.0],
        max_concurrency_values=[8, 64],
    )
    result = _dry_run(config)

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("vllm bench serve") == 2
    assert "--max-concurrency 8" in result.stdout
    assert "--max-concurrency 64" in result.stdout


def test_open_loop_config_emits_no_cap(tmp_path: Path) -> None:
    """With an empty ladder the grid stays open-loop, emitting no in-flight cap."""
    result = _dry_run(_write_config(tmp_path))

    assert result.exit_code == 0, plain(result)
    assert "--max-concurrency" not in result.stdout


def test_config_alignment_floors_the_prefix(tmp_path: Path) -> None:
    """align_blocks floors the prefix to whole blocks in the emitted command."""
    config = _write_config(
        tmp_path, align_blocks=16, prefix_shares=[90], burstiness_values=[1.0]
    )
    result = _dry_run(config)

    assert result.exit_code == 0, plain(result)
    assert "--prefix-repetition-prefix-len 896" in result.stdout
    assert "--prefix-repetition-suffix-len 104" in result.stdout


def test_goodput_reaches_every_cell(tmp_path: Path) -> None:
    """A custom SLO from the config is applied verbatim on every cell."""
    config = _write_config(tmp_path, goodput=["ttft:500", "tpot:25"])
    result = _dry_run(config)

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("--goodput ttft:500 tpot:25") == 6


def test_tokenizer_reaches_every_cell(tmp_path: Path) -> None:
    """A config tokenizer is emitted on every command so vLLM synthesises against it."""
    config = _write_config(tmp_path, tokenizer="Qwen/Qwen2.5-0.5B-Instruct")
    result = _dry_run(config)

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("--tokenizer Qwen/Qwen2.5-0.5B-Instruct") == 6


def test_cli_injects_base_url_model_and_out_dir(tmp_path: Path) -> None:
    """The endpoint, served model, and out-dir come from the CLI, not the config."""
    config = _write_config(tmp_path, prefix_shares=[90], burstiness_values=[1.0])
    result = _dry_run(
        config,
        "--base-url",
        "http://127.0.0.1:9",
        "--model",
        "Qwen/Qwen3-8B-AWQ",
        "--out-dir",
        "/out",
    )

    assert result.exit_code == 0, plain(result)
    assert "--base-url http://127.0.0.1:9" in result.stdout
    assert "--model Qwen/Qwen3-8B-AWQ" in result.stdout
    assert "/out/pshare90_burst1.0.json" in result.stdout


def test_request_rate_inf_from_config_is_accepted(tmp_path: Path) -> None:
    """A request rate of 'inf' is a legitimate unthrottled sweep."""
    config = _write_config(tmp_path, request_rate="inf")
    result = _dry_run(config)

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("--request-rate inf") == 6


# --- load-cell: one cell, its coordinate defined by its config ---------------

# The shared knobs plus one coordinate a cell YAML carries; the CLI injects base_url,
# model, and out_dir on top. Mirrors bench/cell.yaml's documented defaults.
_CELL: dict[str, object] = {
    "share": 90,
    "burstiness": 1.0,
    "total_len": 1000,
    "num_prompts": 500,
    "num_prefixes": 5,
    "output_len": 128,
    "align_blocks": 0,
    "request_rate": 8,
    "seed": 0,
    "goodput": ["ttft:1000", "tpot:50"],
}


def _write_cell(tmp_path: Path, **overrides: object) -> Path:
    """Write a cell config YAML with overrides and return its path."""
    path = tmp_path / "cell.yaml"
    path.write_text(yaml.safe_dump({**_CELL, **overrides}))
    return path


def _cell_dry_run(config: Path, *args: str):
    """Invoke load-cell with the given config, --dry-run, and extra arguments."""
    return runner.invoke(
        app, ["load-cell", "--config", str(config), "--dry-run", *args]
    )


def test_default_cell_dry_run_reads_the_committed_config() -> None:
    """A bare load-cell dry run reads bench/cell.yaml and emits its one documented cell."""
    result = runner.invoke(app, ["load-cell", "--dry-run"])

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("vllm bench serve") == 1
    assert "--model Qwen/Qwen2.5-0.5B-Instruct" in result.stdout
    # bench/cell.yaml's documented coordinate: share 50 (500/500 split), burstiness 1.0.
    assert "--prefix-repetition-prefix-len 500" in result.stdout
    assert "--burstiness 1.0" in result.stdout
    assert "--max-concurrency" not in result.stdout  # default cell is open-loop


def test_load_cell_dry_run_emits_one_command_for_its_coordinate(
    tmp_path: Path,
) -> None:
    """load-cell builds exactly one vllm command for the coordinate its config defines."""
    result = _cell_dry_run(_write_cell(tmp_path, share=90, burstiness=1.0))

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("vllm bench serve") == 1
    assert "--prefix-repetition-prefix-len 900" in result.stdout
    assert "--burstiness 1.0" in result.stdout
    assert "pshare90_burst1.0.json" in result.stdout


def test_load_cell_carries_the_cap_when_closed_loop(tmp_path: Path) -> None:
    """A max_concurrency cell caps in-flight requests and names its _mc file."""
    result = _cell_dry_run(
        _write_cell(tmp_path, share=90, burstiness=1.0, max_concurrency=32)
    )

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("vllm bench serve") == 1
    assert "--max-concurrency 32" in result.stdout
    assert "pshare90_burst1.0_mc32.json" in result.stdout


def test_load_cell_omits_the_cap_when_open_loop(tmp_path: Path) -> None:
    """A cell config with no cap runs open-loop, emitting no --max-concurrency."""
    result = _cell_dry_run(_write_cell(tmp_path, share=90, burstiness=1.0))

    assert result.exit_code == 0, plain(result)
    assert "--max-concurrency" not in result.stdout


def test_load_cell_injects_context_from_the_cli(tmp_path: Path) -> None:
    """The endpoint, served model, and out-dir come from the CLI, not the config."""
    result = _cell_dry_run(
        _write_cell(tmp_path, share=50, burstiness=0.2),
        "--base-url",
        "http://127.0.0.1:9",
        "--model",
        "Qwen/Qwen3-8B-AWQ",
        "--out-dir",
        "/out",
    )

    assert result.exit_code == 0, plain(result)
    assert "--base-url http://127.0.0.1:9" in result.stdout
    assert "--model Qwen/Qwen3-8B-AWQ" in result.stdout
    assert "/out/pshare50_burst0.2.json" in result.stdout


def test_load_cell_runs_one_cell_and_stamps_its_share(
    monkeypatch, tmp_path: Path
) -> None:
    """A live load-cell runs one command, stamps its share, and exits 0."""
    commands: list[list[str]] = []

    def stub_run_cell(command, *, extra_env=None):
        commands.append(command)
        result_file = command[command.index("--result-filename") + 1]
        Path(result_file).write_text(json.dumps({"model_id": "m"}))
        return 0

    monkeypatch.setattr("slipstream_bench.sweep.cli.run_cell", stub_run_cell)

    result = runner.invoke(
        app,
        [
            "load-cell",
            "--config",
            str(_write_cell(tmp_path, share=90, burstiness=1.0)),
            "--out-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(commands) == 1
    written = json.loads((tmp_path / "pshare90_burst1.0.json").read_text())
    assert written["prefix_share"] == 90


def test_load_cell_fails_when_the_cell_errors(monkeypatch, tmp_path: Path) -> None:
    """A cell whose command exits non-zero makes load-cell exit 1."""
    monkeypatch.setattr(
        "slipstream_bench.sweep.cli.run_cell",
        lambda command, *, extra_env=None: 7,
    )

    result = runner.invoke(
        app,
        [
            "load-cell",
            "--config",
            str(_write_cell(tmp_path, share=90, burstiness=1.0)),
            "--out-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1


def test_load_cell_rejects_a_bad_config(tmp_path: Path) -> None:
    """A malformed config exits 2 before any cell runs."""
    result = _cell_dry_run(_write_cell(tmp_path, request_rate="quick"))

    assert result.exit_code == 2
    assert "non-negative number or 'inf'" in plain(result)


def test_load_cell_rejects_an_out_of_range_share(tmp_path: Path) -> None:
    """A share outside 0..100 exits 2 before a cell is built, not a negative suffix."""
    result = _cell_dry_run(_write_cell(tmp_path, share=150))

    assert result.exit_code == 2
    assert "less than or equal to 100" in plain(result)


def test_load_cell_rejects_a_non_positive_cap(tmp_path: Path) -> None:
    """A non-positive max_concurrency exits 2 rather than run a meaningless cell."""
    result = _cell_dry_run(_write_cell(tmp_path, share=50, max_concurrency=0))

    assert result.exit_code == 2
    assert "greater than 0" in plain(result)


def test_load_cell_threads_the_resolved_key_into_the_runner(
    monkeypatch, tmp_path: Path
) -> None:
    """A commercial cell hands the resolved key to run_cell as extra_env, not argv."""
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-live-abc")
    seen: list[dict[str, str] | None] = []

    def stub_run_cell(command, *, extra_env=None):
        seen.append(extra_env)
        result_file = command[command.index("--result-filename") + 1]
        Path(result_file).write_text(json.dumps({"model_id": "m"}))
        return 0

    monkeypatch.setattr("slipstream_bench.sweep.cli.run_cell", stub_run_cell)

    result = runner.invoke(
        app,
        [
            "load-cell",
            "--config",
            str(
                _write_cell(
                    tmp_path,
                    share=50,
                    burstiness=1.0,
                    tokenizer="Qwen/Qwen2.5-0.5B-Instruct",
                )
            ),
            "--api-key-env",
            "MY_PROVIDER_KEY",
            "--out-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [{"OPENAI_API_KEY": "sk-live-abc"}]


# --- a malformed config is rejected before any command is built --------------


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"prefix_shares": [150]}, "less than or equal to 100"),
        ({"prefix_shares": [50, 50]}, "unique"),
        ({"prefix_shares": []}, "prefix_shares"),
        ({"max_concurrency_values": [0]}, "greater than 0"),
        ({"num_prompts": 2, "num_prefixes": 5}, "below num_prefixes"),
        ({"request_rate": "quick"}, "non-negative number or 'inf'"),
        ({"unknown_knob": 1}, "Extra inputs are not permitted"),
        (
            {
                "align_blocks": 16,
                "total_len": 100,
                "prefix_shares": [10],
                "burstiness_values": [1.0],
            },
            "floors prefix",
        ),
    ],
)
def test_bad_config_exits_two(
    tmp_path: Path, overrides: dict[str, object], needle: str
) -> None:
    """A malformed config exits 2 with a diagnostic naming the offending knob."""
    result = _dry_run(_write_config(tmp_path, **overrides))

    assert result.exit_code == 2
    assert needle in plain(result)


@pytest.mark.parametrize("key", ["base_url", "model", "out_dir", "commercial"])
def test_config_setting_a_reserved_key_exits_two(tmp_path: Path, key: str) -> None:
    """A config that sets a CLI-injected key exits 2; model.yaml is the model SoT."""
    result = _dry_run(_write_config(tmp_path, **{key: "x"}))

    assert result.exit_code == 2
    assert key in plain(result)


def test_missing_config_exits_two(tmp_path: Path) -> None:
    """A --config path that does not exist is rejected by the option guard."""
    result = runner.invoke(
        app, ["load-sweep", "--config", str(tmp_path / "absent.yaml"), "--dry-run"]
    )

    assert result.exit_code == 2


# --- the cell runner and api-key resolver ------------------------------------


def test_run_cell_reports_a_missing_binary_clearly() -> None:
    """A missing binary fails fast with an actionable message, not a traceback."""
    with pytest.raises(SweepError, match="cannot run 'definitely-not-a-real-binary"):
        run_cell(["definitely-not-a-real-binary-xyz", "--flag"])


def test_run_cell_reports_a_non_executable_binary_clearly(tmp_path: Path) -> None:
    """A present-but-not-executable binary fails fast, not as a raw traceback."""
    not_exec = tmp_path / "vllm"
    not_exec.write_text("#!/bin/sh\n", encoding="utf-8")
    not_exec.chmod(0o644)

    with pytest.raises(SweepError, match=f"cannot run '{not_exec}'"):
        run_cell([str(not_exec), "bench", "serve"])


def test_run_cell_injects_extra_env_into_the_child() -> None:
    """A resolved key reaches the child process environment, not the command line."""
    assert (
        run_cell(
            ["sh", "-c", 'test "$OPENAI_API_KEY" = secret'],
            extra_env={"OPENAI_API_KEY": "secret"},
        )
        == 0
    )


def test_run_cell_leaves_the_child_env_untouched_without_extra_env() -> None:
    """With no extra env the child inherits the parent unchanged; the key is absent."""
    assert run_cell(["sh", "-c", 'test -z "$OPENAI_API_KEY"'], extra_env=None) == 0


def test_resolve_api_key_env_maps_the_named_var_to_openai_api_key(monkeypatch) -> None:
    """The named env var's value is handed to the child as OPENAI_API_KEY."""
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-live-abc")

    assert resolve_api_key_env("MY_PROVIDER_KEY") == {"OPENAI_API_KEY": "sk-live-abc"}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_resolve_api_key_env_rejects_an_unset_or_empty_var(monkeypatch, value) -> None:
    """An unset or blank key fails fast rather than sending an unauthenticated run."""
    if value is None:
        monkeypatch.delenv("MY_PROVIDER_KEY", raising=False)
    else:
        monkeypatch.setenv("MY_PROVIDER_KEY", value)

    with pytest.raises(SweepError, match="MY_PROVIDER_KEY"):
        resolve_api_key_env("MY_PROVIDER_KEY")


# --- the commercial arm threads the key through the environment --------------


def test_dry_run_never_prints_the_api_key(monkeypatch, tmp_path) -> None:
    """The key is env-only: --api-key-env leaves no secret in the echoed commands."""
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-live-should-not-leak")
    config = _write_config(tmp_path, tokenizer="Qwen/Qwen2.5-0.5B-Instruct")

    result = _dry_run(config, "--api-key-env", "MY_PROVIDER_KEY")

    assert result.exit_code == 0, plain(result)
    assert "sk-live-should-not-leak" not in result.stdout
    assert "MY_PROVIDER_KEY" not in result.stdout


def test_unset_api_key_env_is_rejected_before_the_sweep(monkeypatch) -> None:
    """A live run naming an unset key var exits 2 before any cell runs vLLM."""
    monkeypatch.delenv("MY_PROVIDER_KEY", raising=False)

    result = runner.invoke(app, ["load-sweep", "--api-key-env", "MY_PROVIDER_KEY"])

    assert result.exit_code == 2
    assert "MY_PROVIDER_KEY" in plain(result)


def test_dry_run_with_an_unset_api_key_env_succeeds(monkeypatch, tmp_path) -> None:
    """A dry run previews a commercial sweep without the key being exported."""
    monkeypatch.delenv("MY_PROVIDER_KEY", raising=False)
    config = _write_config(tmp_path, tokenizer="Qwen/Qwen2.5-0.5B-Instruct")

    result = _dry_run(config, "--api-key-env", "MY_PROVIDER_KEY")

    assert result.exit_code == 0, plain(result)
    assert result.stdout.count("vllm bench serve") == 6


def test_live_sweep_threads_the_resolved_key_into_the_runner(
    monkeypatch, tmp_path
) -> None:
    """A non-dry sweep hands the resolved key to run_cell as extra_env, not argv."""
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-live-abc")
    config = _write_config(
        tmp_path,
        prefix_shares=[50],
        burstiness_values=[1.0],
        tokenizer="Qwen/Qwen2.5-0.5B-Instruct",
    )
    seen: list[dict[str, str] | None] = []

    def stub_run_cell(command, *, extra_env=None):
        seen.append(extra_env)
        # A clean cell writes a stampable result file, as vLLM would.
        result_file = command[command.index("--result-filename") + 1]
        Path(result_file).write_text(json.dumps({"model_id": "m"}))
        return 0

    monkeypatch.setattr("slipstream_bench.sweep.cli.run_cell", stub_run_cell)

    result = runner.invoke(
        app,
        [
            "load-sweep",
            "--config",
            str(config),
            "--api-key-env",
            "MY_PROVIDER_KEY",
            "--out-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [{"OPENAI_API_KEY": "sk-live-abc"}]


def test_live_commercial_sweep_without_a_tokenizer_is_rejected(
    monkeypatch, tmp_path
) -> None:
    """The key resolves first; the tokenizer guard must still fire with no cell run."""
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-live-abc")
    config = _write_config(tmp_path)  # no tokenizer

    def stub_run_cell(command, *, extra_env=None):
        raise AssertionError(
            "no cell should run when the tokenizer guard rejects the sweep"
        )

    monkeypatch.setattr("slipstream_bench.sweep.cli.run_cell", stub_run_cell)

    result = runner.invoke(
        app,
        ["load-sweep", "--config", str(config), "--api-key-env", "MY_PROVIDER_KEY"],
    )

    assert result.exit_code == 2
    assert "tokenizer" in plain(result)
