"""Tests for the orchestration grid helper: resolve one engine point's vLLM args.

Exercises get_engine_args — the single source of a Tier-1 point's engine args the worker
renders into the GPU manifest: max-num-seqs from the point, the --kv-cache-dtype token
from the grid's label->token mapping (fp16 -> float16), and the prefix-caching flag from
the point's arm. A point the grid does not sweep never resolves args — it raises.
"""

from pathlib import Path

import pytest
import yaml

from slipstream_bench.contract import EnginePoint, SweepGridError, load_grid
from slipstream_bench.orchestration.grid import get_engine_args

_LOAD = {
    "total_len": 1000,
    "num_prompts": 500,
    "num_prefixes": 5,
    "output_len": 128,
    "align_blocks": 0,
    "request_rate": 8,
    "seed": 0,
    "goodput": ["ttft:1000", "tpot:50"],
}


def _valid_grid() -> dict[str, object]:
    return {
        "tier1": {
            "max_num_seqs": [16, 32, 64, 128, 256],
            "kv_cache_dtype": ["fp8", "fp16"],
            "prefix_caching": {
                "on": {"flag": "--enable-prefix-caching", "prefix_share": [10, 50, 90]},
                "off": {"flag": "--no-enable-prefix-caching", "prefix_share": [0]},
            },
        },
        "tier2": {"max_concurrency": [8, 16, 32, 64, 128, 256], "burstiness": 1.0},
        "load": dict(_LOAD),
    }


def _write_grid(tmp_path: Path, grid: dict[str, object]) -> Path:
    path = tmp_path / "grid.yaml"
    path.write_text(yaml.safe_dump(grid))
    return path


def test_engine_args_resolve_a_points_vllm_args(tmp_path: Path) -> None:
    """A caching-on fp8 point resolves max-num-seqs, the fp8 token, and the on flag."""
    grid = load_grid(_write_grid(tmp_path, _valid_grid()))
    point = EnginePoint(max_num_seqs=64, kv_cache_dtype="fp8", prefix_caching=True)

    engine_args = get_engine_args(grid, point)

    assert engine_args.max_num_seqs == 64
    assert engine_args.kv_engine_token == "fp8"
    assert engine_args.prefix_caching_flag == "--enable-prefix-caching"


def test_engine_args_map_fp16_and_the_caching_off_flag(tmp_path: Path) -> None:
    """fp16 resolves vLLM's float16 token; the off arm resolves the disable flag."""
    grid = load_grid(_write_grid(tmp_path, _valid_grid()))
    point = EnginePoint(max_num_seqs=32, kv_cache_dtype="fp16", prefix_caching=False)

    engine_args = get_engine_args(grid, point)

    assert engine_args.kv_engine_token == "float16"
    assert engine_args.prefix_caching_flag == "--no-enable-prefix-caching"


def test_engine_args_reject_a_point_absent_from_the_grid(tmp_path: Path) -> None:
    """A point the grid does not sweep never resolves args — it raises."""
    grid = load_grid(_write_grid(tmp_path, _valid_grid()))
    absent = EnginePoint(max_num_seqs=999, kv_cache_dtype="fp8", prefix_caching=True)

    with pytest.raises(SweepGridError):
        get_engine_args(grid, absent)
