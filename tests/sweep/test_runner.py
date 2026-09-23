"""Tests for the serve-sweep command builder, split math, and grid orchestration.

Covers the edge cases: integer prefix/suffix truncation, block alignment and its
collapse guard, a fixed seed replaying every cell, one ``vllm bench serve`` per
grid cell, the SLO on every cell, distinct per-cell result files, and
partial-failure survival. The command-assembly seam is driven through the pure
builder functions, with no vLLM server.
"""

import json
from pathlib import Path

import pytest

from slipstream_bench.sweep.config import SweepConfig, SweepError
from slipstream_bench.sweep.runner import (
    cell_command,
    grid,
    run_sweep,
    split_lengths,
)


def _config(**overrides: object) -> SweepConfig:
    """Build a SweepConfig from the documented defaults, with overrides applied."""
    base = {
        "base_url": "http://localhost:8000",
        "model": "Qwen/Qwen2.5-0.5B-Instruct",
        "prefix_shares": [10, 50, 90],
        "burstiness_values": [0.2, 1.0],
        "total_len": 1000,
        "num_prompts": 100,
        "num_prefixes": 5,
        "output_len": 128,
        "align_blocks": 0,
        "request_rate": "8",
        "seed": 0,
        "out_dir": "bench/results",
        "goodput": ["ttft:1000", "tpot:50"],
    }
    base.update(overrides)
    return SweepConfig(**base)  # type: ignore[arg-type]


# --- split_lengths: integer truncation and block alignment -------------------


@pytest.mark.parametrize(
    ("total_len", "share", "expected"),
    [
        (1000, 25, (250, 750)),
        (1000, 90, (900, 100)),
        (1000, 0, (0, 1000)),
        (1000, 100, (1000, 0)),
        (100, 33, (33, 67)),
    ],
)
def test_split_truncates_to_integers(
    total_len: int, share: int, expected: tuple[int, int]
) -> None:
    """Prefix-share splits a fixed budget, flooring to whole tokens."""
    assert split_lengths(total_len, share, align_blocks=0) == expected


@pytest.mark.parametrize(
    ("total_len", "share", "align", "expected"),
    [
        (1000, 90, 16, (896, 104)),
        (1000, 10, 16, (96, 904)),
        (1000, 50, 16, (496, 504)),
        (512, 50, 16, (256, 256)),
        (1000, 100, 16, (992, 8)),
        (1000, 0, 16, (0, 1000)),
    ],
)
def test_alignment_floors_prefix_to_whole_blocks(
    total_len: int, share: int, align: int, expected: tuple[int, int]
) -> None:
    """``align_blocks`` floors a non-empty prefix to a multiple of the block size."""
    assert split_lengths(total_len, share, align_blocks=align) == expected


def test_alignment_off_keeps_raw_split() -> None:
    """align_blocks of 0 opts out: the raw truncated split passes through."""
    assert split_lengths(1000, 90, align_blocks=0) == (900, 100)


def test_alignment_that_erases_prefix_fails_fast() -> None:
    """Flooring a non-empty prefix to zero erases what the run measures."""
    with pytest.raises(SweepError, match="floors prefix"):
        split_lengths(100, 10, align_blocks=16)


# --- SweepConfig invariants: no empty grid, shares in range --------------------


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"prefix_shares": []}, "prefix-shares"),
        ({"burstiness_values": []}, "burstiness"),
        ({"goodput": []}, "goodput"),
        ({"prefix_shares": [150]}, "outside 0..100"),
        ({"num_prompts": 2, "num_prefixes": 5}, "num-prompts"),
        ({"commercial": True}, "tokenizer"),
    ],
)
def test_config_rejects_a_meaningless_sweep(
    overrides: dict[str, object], match: str
) -> None:
    """An empty grid axis, empty SLO, or out-of-range share fails at construction."""
    with pytest.raises(SweepError, match=match):
        _config(**overrides)


# --- commercial arm: a local tokenizer, not the provider model id ------------


def test_commercial_sweep_requires_a_tokenizer() -> None:
    """A commercial run cannot resolve --model as an HF tokenizer; --tokenizer is required."""
    with pytest.raises(SweepError, match="tokenizer"):
        _config(commercial=True)


def test_commercial_sweep_with_a_tokenizer_is_valid() -> None:
    """A commercial run pinned to a local tokenizer constructs without error."""
    _config(commercial=True, tokenizer="Qwen/Qwen2.5-0.5B-Instruct")


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_commercial_sweep_rejects_a_blank_tokenizer(blank: str) -> None:
    """A whitespace-only tokenizer pins no real ruler and dies in every cell; reject upfront."""
    with pytest.raises(SweepError, match="tokenizer"):
        _config(commercial=True, tokenizer=blank)


def test_cell_command_carries_the_tokenizer_when_set() -> None:
    """A configured tokenizer reaches the command so vLLM synthesises against it."""
    cfg = _config(tokenizer="Qwen/Qwen2.5-0.5B-Instruct")
    joined = " ".join(cell_command(cfg, share=90, burstiness=1.0))
    assert "--tokenizer Qwen/Qwen2.5-0.5B-Instruct" in joined


def test_cell_command_omits_the_tokenizer_when_unset() -> None:
    """The self-hosted arm leaves --tokenizer off; vLLM defaults it to --model."""
    joined = " ".join(cell_command(_config(), share=90, burstiness=1.0))
    assert "--tokenizer" not in joined


# --- grid: one cell per (share, burstiness) ----------------------------------


def test_grid_is_the_cartesian_product_in_order() -> None:
    """The grid yields every (share, burstiness, max-concurrency) cell, shares outermost.

    With no closed-loop ladder configured, the max-concurrency axis contributes a
    single open-loop ``None``, so the grid is one cell per (share, burstiness) pair.
    """
    cfg = _config(prefix_shares=[10, 90], burstiness_values=[0.2, 1.0])
    assert list(grid(cfg)) == [
        (10, 0.2, None),
        (10, 1.0, None),
        (90, 0.2, None),
        (90, 1.0, None),
    ]


def test_grid_ladders_max_concurrency_innermost() -> None:
    """The ladder is innermost: its rungs run contiguously within one (share, burstiness).

    Varying all three axes (2 shares x 2 burstiness x 2 rungs) is what pins the
    nesting — with a single value on the other two axes the ladder's position in
    the product would be unobservable. Shares stay outermost, ladder innermost.
    """
    cfg = _config(
        prefix_shares=[10, 90],
        burstiness_values=[0.2, 1.0],
        max_concurrency_values=[8, 16],
    )
    assert list(grid(cfg)) == [
        (10, 0.2, 8),
        (10, 0.2, 16),
        (10, 1.0, 8),
        (10, 1.0, 16),
        (90, 0.2, 8),
        (90, 0.2, 16),
        (90, 1.0, 8),
        (90, 1.0, 16),
    ]


@pytest.mark.parametrize("cap", [0, -1])
def test_config_rejects_a_nonpositive_max_concurrency(cap: int) -> None:
    """A max-concurrency of zero or below caps in-flight requests at none; reject it."""
    with pytest.raises(SweepError, match="max-concurrency"):
        _config(max_concurrency_values=[cap])


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"prefix_shares": [10, 50, 10]}, "duplicate prefix-share 10"),
        ({"prefix_shares": [10, 50, 10, 50]}, "duplicate prefix-share 10"),
        ({"burstiness_values": [0.2, 1.0, 0.2]}, "duplicate burstiness 0.2"),
        ({"max_concurrency_values": [8, 16, 8]}, "duplicate max-concurrency 8"),
    ],
)
def test_config_rejects_a_duplicate_axis_value(
    overrides: dict[str, object], match: str
) -> None:
    """A repeated grid-axis value reruns an identical cell; reject it at construction."""
    with pytest.raises(SweepError, match=match):
        _config(**overrides)


# --- cell_command: the flag assembly the retired bash test pinned ------------


def test_cell_command_carries_the_split_slo_and_result_file() -> None:
    """A cell command wires the split, the SLO, and a distinct result file."""
    cfg = _config()
    cmd = cell_command(cfg, share=90, burstiness=1.0)
    joined = " ".join(cmd)

    assert "vllm bench serve" in joined
    assert "--base-url http://localhost:8000" in joined
    assert "--model Qwen/Qwen2.5-0.5B-Instruct" in joined
    assert "--dataset-name prefix_repetition" in joined
    assert "--prefix-repetition-prefix-len 900" in joined
    assert "--prefix-repetition-suffix-len 100" in joined
    assert "--prefix-repetition-num-prefixes 5" in joined
    assert "--prefix-repetition-output-len 128" in joined
    assert "--num-prompts 100" in joined
    assert "--request-rate 8" in joined
    assert "--seed 0" in joined
    assert "--burstiness 1.0" in joined
    assert "--goodput ttft:1000 tpot:50" in joined
    assert "--percentile-metrics ttft,tpot,itl,e2el" in joined
    assert "--metric-percentiles 95,99" in joined
    assert "--save-result" in joined
    assert "--save-detailed" in joined
    assert "bench/results/pshare90_burst1.0.json" in joined


def test_cell_command_is_reproducible_for_a_fixed_config() -> None:
    """The same config builds identical commands, carrying the same fixed seed."""
    cfg = _config()
    first = cell_command(cfg, share=90, burstiness=1.0)
    second = cell_command(cfg, share=90, burstiness=1.0)

    assert first == second
    assert "--seed 0" in " ".join(first)


def test_cell_command_result_file_is_distinct_per_cell() -> None:
    """Each cell names a result file from its share and burstiness."""
    cfg = _config(out_dir="/tmp/slipstream-bench")
    low = " ".join(cell_command(cfg, share=25, burstiness=0.2))
    high = " ".join(cell_command(cfg, share=90, burstiness=1.0))

    assert "/tmp/slipstream-bench/pshare25_burst0.2.json" in low
    assert "/tmp/slipstream-bench/pshare90_burst1.0.json" in high


def test_cell_command_carries_max_concurrency_when_set() -> None:
    """A closed-loop cell caps in-flight requests and names a result file by the cap."""
    cfg = _config(out_dir="/tmp/slipstream-bench")
    joined = " ".join(cell_command(cfg, share=90, burstiness=1.0, max_concurrency=32))

    assert "--max-concurrency 32" in joined
    assert "/tmp/slipstream-bench/pshare90_burst1.0_mc32.json" in joined


def test_cell_command_omits_max_concurrency_when_open_loop() -> None:
    """An open-loop cell (no cap) leaves --max-concurrency off and its file un-suffixed."""
    joined = " ".join(cell_command(_config(), share=90, burstiness=1.0))

    assert "--max-concurrency" not in joined
    assert "pshare90_burst1.0.json" in joined


# --- run_sweep: dry run, survival, and per-cell tally ------------------------


def test_dry_run_prints_every_cell_and_runs_none() -> None:
    """--dry-run echoes one command per cell and never invokes the runner."""
    cfg = _config()
    printed: list[str] = []
    calls: list[list[str]] = []

    code = run_sweep(
        cfg,
        dry_run=True,
        runner=lambda cmd: (calls.append(cmd), 0)[1],
        echo=printed.append,
        warn=printed.append,
    )

    assert code == 0
    assert calls == []
    assert sum(line.count("vllm bench serve") for line in printed) == 6
    assert sum(line.count("--goodput ttft:1000 tpot:50") for line in printed) == 6
    assert sum(line.count("--seed 0") for line in printed) == 6


def test_run_sweep_invokes_the_runner_once_per_cell(tmp_path) -> None:
    """A live run fires one runner call per grid cell and succeeds when all pass."""
    cfg = _config(out_dir=str(tmp_path))
    calls: list[list[str]] = []

    def runner(command: list[str]) -> int:
        calls.append(command)
        # A clean success writes a stampable result file, as vLLM would.
        Path(_result_filename(command)).write_text(json.dumps({"model_id": "m"}))
        return 0

    code = run_sweep(
        cfg,
        dry_run=False,
        runner=runner,
        echo=lambda _line: None,
        warn=lambda _line: None,
    )

    assert code == 0
    assert len(calls) == 6


def test_run_sweep_ladders_max_concurrency_into_distinct_files(tmp_path) -> None:
    """A closed-loop ladder runs each rung to its own file and labels the progress line.

    Guards the integration seam the unit tests only prove piecewise: multiple rungs
    of one (share, burstiness) pair write distinct ``_mc{N}`` files (never colliding)
    and the operator's progress line carries the cap it is running.
    """
    cfg = _config(
        prefix_shares=[90],
        burstiness_values=[1.0],
        max_concurrency_values=[8, 64],
        out_dir=str(tmp_path),
    )
    echoed: list[str] = []

    def runner(command: list[str]) -> int:
        Path(_result_filename(command)).write_text(json.dumps({"model_id": "m"}))
        return 0

    code = run_sweep(
        cfg,
        dry_run=False,
        runner=runner,
        echo=echoed.append,
        warn=lambda _line: None,
    )

    assert code == 0
    written = sorted(p.name for p in tmp_path.glob("*.json"))
    assert written == ["pshare90_burst1.0_mc64.json", "pshare90_burst1.0_mc8.json"]
    # Each rung's progress line names the cap it is running.
    assert any("max-concurrency 8" in line for line in echoed)
    assert any("max-concurrency 64" in line for line in echoed)


def _result_filename(command: list[str]) -> str:
    """Pull the --result-filename a cell command writes to, as vLLM would."""
    return command[command.index("--result-filename") + 1]


def test_successful_cell_gets_its_prefix_share_injected(tmp_path) -> None:
    """After a cell writes its result JSON, the sweep injects that cell's share."""
    cfg = _config(prefix_shares=[90], burstiness_values=[1.0], out_dir=str(tmp_path))

    def runner(command: list[str]) -> int:
        # Stand in for vLLM: write the raw client JSON the flag names, no share.
        Path(_result_filename(command)).write_text(json.dumps({"model_id": "m"}))
        return 0

    code = run_sweep(
        cfg,
        dry_run=False,
        runner=runner,
        echo=lambda _line: None,
        warn=lambda _line: None,
    )

    assert code == 0
    written = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert written["prefix_share"] == 90
    # The metric vLLM wrote survives the injection.
    assert written["model_id"] == "m"


def test_unannotatable_cell_warns_and_fails_the_sweep(tmp_path) -> None:
    """A cell that ran but could not be stamped is not a clean success: warn + fail."""
    cfg = _config(prefix_shares=[90], burstiness_values=[1.0], out_dir=str(tmp_path))
    warned: list[str] = []

    code = run_sweep(
        cfg,
        dry_run=False,
        runner=lambda _cmd: 0,  # ran ok but wrote no result file to stamp
        echo=lambda _line: None,
        warn=warned.append,
    )

    assert code == 1
    assert any("prefix-share" in line for line in warned)
    # The tally is printed even though every runner exited 0.
    assert any("un-annotated" in line for line in warned)


def test_non_object_result_file_is_not_annotated(tmp_path) -> None:
    """A result file that is a JSON array, not an object, warns and fails the sweep."""
    cfg = _config(prefix_shares=[90], burstiness_values=[1.0], out_dir=str(tmp_path))
    warned: list[str] = []

    def runner(command: list[str]) -> int:
        Path(_result_filename(command)).write_text(json.dumps([1, 2, 3]))
        return 0

    code = run_sweep(
        cfg,
        dry_run=False,
        runner=runner,
        echo=lambda _line: None,
        warn=warned.append,
    )

    assert code == 1
    assert any("not a JSON object" in line for line in warned)


def test_run_sweep_creates_a_nested_out_dir(tmp_path) -> None:
    """A live run creates the out-dir, including missing parents."""
    out_dir = tmp_path / "results" / "run1"
    cfg = _config(out_dir=str(out_dir))

    run_sweep(
        cfg,
        dry_run=False,
        runner=lambda _cmd: 0,
        echo=lambda _line: None,
        warn=lambda _line: None,
    )

    assert out_dir.is_dir()


def test_failing_cell_does_not_abort_the_grid(tmp_path) -> None:
    """A cell's failure is tallied; the remaining cells still run; exit is non-zero."""
    cfg = _config(
        prefix_shares=[10, 50, 90], burstiness_values=[1.0], out_dir=str(tmp_path)
    )
    attempted: list[str] = []

    warned: list[str] = []

    def runner(cmd: list[str]) -> int:
        # The middle cell (share 50) fails; the other two must still be attempted.
        joined = " ".join(cmd)
        attempted.append(joined)
        return 7 if "pshare50_" in joined else 0

    code = run_sweep(
        cfg,
        dry_run=False,
        runner=runner,
        echo=lambda _line: None,
        warn=warned.append,
    )

    assert code == 1
    assert len(attempted) == 3
    # The failure line names the offending cell and carries its exit code.
    assert any("burstiness 1.0 failed (exit 7)" in line for line in warned)
    assert any("2 cells ok, 1 failed" in line for line in warned)
