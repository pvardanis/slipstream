"""Tests for the serve-sweep command builder, split math, and grid orchestration.

Covers the edge cases: integer prefix/suffix truncation, block alignment and its
collapse guard, a fixed seed replaying every cell, one ``vllm bench serve`` per
grid cell, the SLO on every cell, distinct per-cell result files, and
partial-failure survival. The command-assembly seam is driven through the pure
builder functions, with no vLLM server.
"""

import pytest

from slipstream_bench.serve_sweep import (
    SweepConfig,
    SweepError,
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


# --- grid: one cell per (share, burstiness) ----------------------------------


def test_grid_is_the_cartesian_product_in_order() -> None:
    """The grid yields every (share, burstiness) pair, shares outermost."""
    cfg = _config(prefix_shares=[10, 90], burstiness_values=[0.2, 1.0])
    assert list(grid(cfg)) == [(10, 0.2), (10, 1.0), (90, 0.2), (90, 1.0)]


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
    """The same config builds byte-identical commands, so a run replays exactly."""
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

    code = run_sweep(
        cfg,
        dry_run=False,
        runner=lambda cmd: (calls.append(cmd), 0)[1],
        echo=lambda _line: None,
    )

    assert code == 0
    assert len(calls) == 6


def test_failing_cell_does_not_abort_the_grid(tmp_path) -> None:
    """A cell's failure is tallied; the remaining cells still run; exit is non-zero."""
    cfg = _config(
        prefix_shares=[10, 50, 90], burstiness_values=[1.0], out_dir=str(tmp_path)
    )
    attempted: list[str] = []

    def runner(cmd: list[str]) -> int:
        # The middle cell (share 50) fails; the other two must still be attempted.
        joined = " ".join(cmd)
        attempted.append(joined)
        return 1 if "pshare50_" in joined else 0

    code = run_sweep(cfg, dry_run=False, runner=runner, echo=lambda _line: None)

    assert code == 1
    assert len(attempted) == 3
