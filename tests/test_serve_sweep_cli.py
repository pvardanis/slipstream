"""Tests for the serve-sweep CLI surface: dry-run assembly and input rejection.

Drives the command-construction seam through ``--dry-run`` (no vLLM, no cluster)
and pins that a bad argument is rejected with a non-zero exit before any command
is built.
"""

import pytest
from typer.testing import CliRunner

from slipstream_bench.cli import app
from slipstream_bench.cli_helpers import resolve_api_key_env, run_cell
from slipstream_bench.serve_sweep import SweepError

runner = CliRunner()


def _dry_run(*args: str):
    """Invoke serve-sweep with --dry-run and the given extra arguments."""
    return runner.invoke(app, ["serve-sweep", "--dry-run", *args])


def test_default_dry_run_emits_the_documented_grid() -> None:
    """A zero-override dry run emits the 3x2 default grid with its defaults."""
    result = _dry_run()

    assert result.exit_code == 0
    assert result.stdout.count("vllm bench serve") == 6
    assert result.stdout.count("--goodput ttft:1000 tpot:50") == 6
    assert result.stdout.count("--seed 0") == 6
    assert "--model Qwen/Qwen2.5-0.5B-Instruct" in result.stdout
    assert "--prefix-repetition-prefix-len 100" in result.stdout
    assert "--prefix-repetition-prefix-len 500" in result.stdout
    assert "--prefix-repetition-prefix-len 900" in result.stdout
    assert result.stdout.count("--burstiness 0.2") == 3
    assert result.stdout.count("--burstiness 1.0") == 3


def test_dry_run_honours_grid_overrides() -> None:
    """Repeatable share/burstiness options define the grid and the split."""
    result = _dry_run(
        "--prefix-share",
        "25",
        "--prefix-share",
        "50",
        "--prefix-share",
        "90",
        "--burstiness",
        "0.2",
        "--burstiness",
        "1.0",
        "--total-len",
        "1000",
    )

    assert result.exit_code == 0
    assert result.stdout.count("vllm bench serve") == 6
    assert "--prefix-repetition-prefix-len 250" in result.stdout
    assert "--prefix-repetition-suffix-len 750" in result.stdout


def test_dry_run_applies_alignment() -> None:
    """--align-blocks floors the prefix to whole blocks in the emitted command."""
    result = _dry_run(
        "--align-blocks",
        "16",
        "--total-len",
        "1000",
        "--prefix-share",
        "90",
        "--burstiness",
        "1.0",
    )

    assert result.exit_code == 0
    assert "--prefix-repetition-prefix-len 896" in result.stdout
    assert "--prefix-repetition-suffix-len 104" in result.stdout


def test_goodput_override_reaches_every_cell() -> None:
    """A custom SLO is applied verbatim on every cell."""
    result = _dry_run("--goodput", "ttft:500", "--goodput", "tpot:25")

    assert result.exit_code == 0
    assert result.stdout.count("--goodput ttft:500 tpot:25") == 6


def test_seed_override_reaches_every_cell() -> None:
    """A custom seed replays on every cell."""
    result = _dry_run("--seed", "123")

    assert result.exit_code == 0
    assert result.stdout.count("--seed 123") == 6


def test_non_numeric_total_len_is_rejected() -> None:
    """A non-integer --total-len is rejected before any command is built."""
    result = _dry_run("--total-len", "foo")
    assert result.exit_code != 0


def test_zero_total_len_is_rejected() -> None:
    """--total-len must be a positive integer."""
    result = _dry_run("--total-len", "0")
    assert result.exit_code == 2
    assert "--total-len" in result.output


def test_share_above_100_is_rejected() -> None:
    """A prefix-share outside 0..100 is rejected with a diagnostic."""
    result = _dry_run("--prefix-share", "150")
    assert result.exit_code == 2
    assert "prefix-share" in result.output


def test_negative_share_is_rejected() -> None:
    """A negative prefix-share is rejected."""
    result = _dry_run("--prefix-share", "-5")
    assert result.exit_code != 0


def test_empty_goodput_is_rejected() -> None:
    """An empty SLO is rejected."""
    result = _dry_run("--goodput", "")
    assert result.exit_code == 2
    assert "goodput" in result.output


def test_goodput_drops_empty_tokens_and_keeps_the_rest() -> None:
    """An empty token is filtered out; a non-empty one still reaches the command."""
    result = _dry_run("--goodput", "", "--goodput", "ttft:1000")
    assert result.exit_code == 0
    assert result.stdout.count("--goodput ttft:1000") == 6
    # The dropped empty token must not leave a dangling separator on the command.
    assert "--goodput  " not in result.stdout


def test_negative_align_blocks_is_rejected() -> None:
    """--align-blocks must be non-negative."""
    result = _dry_run("--align-blocks", "-16")
    assert result.exit_code != 0


def test_alignment_that_erases_prefix_is_rejected() -> None:
    """Alignment flooring a non-empty prefix to zero fails fast with a diagnostic."""
    result = _dry_run(
        "--align-blocks",
        "16",
        "--total-len",
        "100",
        "--prefix-share",
        "10",
        "--burstiness",
        "1.0",
    )
    assert result.exit_code == 2
    assert "floors prefix" in result.output


def test_request_rate_inf_is_accepted() -> None:
    """A request rate of 'inf' is a legitimate unthrottled sweep."""
    result = _dry_run("--request-rate", "inf")
    assert result.exit_code == 0
    assert result.stdout.count("--request-rate inf") == 6


def test_non_numeric_request_rate_is_rejected() -> None:
    """A request rate that is neither a number nor 'inf' is rejected."""
    result = _dry_run("--request-rate", "quick")
    assert result.exit_code == 2
    assert "request-rate" in result.output


def test_negative_request_rate_is_rejected() -> None:
    """A negative request rate is meaningless and is rejected before vLLM sees it."""
    result = _dry_run("--request-rate", "-8")
    assert result.exit_code == 2
    assert "request-rate" in result.output


def test_nan_request_rate_is_rejected() -> None:
    """A non-finite request rate does not slip through the float coercion."""
    result = _dry_run("--request-rate", "nan")
    assert result.exit_code == 2
    assert "request-rate" in result.output


def test_fewer_prompts_than_prefixes_is_rejected() -> None:
    """Fewer prompts than prefixes fails fast before any cell runs vLLM."""
    result = _dry_run("--num-prompts", "2", "--num-prefixes", "5")
    assert result.exit_code == 2
    assert "num-prompts" in result.output


def test_run_cell_reports_a_missing_binary_clearly() -> None:
    """A missing binary fails fast with an actionable message, not a traceback."""
    with pytest.raises(SweepError, match="not found on PATH"):
        run_cell(["definitely-not-a-real-binary-xyz", "--flag"])


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


@pytest.mark.parametrize("value", [None, ""])
def test_resolve_api_key_env_rejects_an_unset_or_empty_var(monkeypatch, value) -> None:
    """An unset or blank key fails fast rather than sending an unauthenticated run."""
    if value is None:
        monkeypatch.delenv("MY_PROVIDER_KEY", raising=False)
    else:
        monkeypatch.setenv("MY_PROVIDER_KEY", value)

    with pytest.raises(SweepError, match="MY_PROVIDER_KEY"):
        resolve_api_key_env("MY_PROVIDER_KEY")


def test_dry_run_never_prints_the_api_key(monkeypatch) -> None:
    """The key is env-only: --api-key-env leaves no secret in the echoed commands."""
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-live-should-not-leak")

    result = _dry_run("--api-key-env", "MY_PROVIDER_KEY")

    assert result.exit_code == 0
    assert "sk-live-should-not-leak" not in result.stdout
    assert "MY_PROVIDER_KEY" not in result.stdout


def test_unset_api_key_env_is_rejected_before_the_sweep(monkeypatch) -> None:
    """A live run naming an unset key var exits 2 before any cell runs vLLM."""
    monkeypatch.delenv("MY_PROVIDER_KEY", raising=False)

    result = runner.invoke(app, ["serve-sweep", "--api-key-env", "MY_PROVIDER_KEY"])

    assert result.exit_code == 2
    assert "MY_PROVIDER_KEY" in result.output
