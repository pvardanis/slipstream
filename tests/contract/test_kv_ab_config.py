"""Tests for the kv-cache-extension-ab workload config: its load and its validation.

The config holds the workload both arms run — the fixed engine knobs, the prefix share,
the concurrency ladder, the warmup rung, the runs per arm, and the shared load knobs —
and is validated on load, so a bad workload fails before any GPU redeploy.
"""

from pathlib import Path

import pytest
import yaml

from slipstream_bench.contract.kv_ab_config import (
    KvAbConfigError,
    load_kv_ab_config,
)

_COMMITTED_CONFIG = Path(__file__).parents[2] / "bench" / "kv-cache-extension-ab.yaml"


def test_the_committed_config_loads_with_its_block_aligned_working_set() -> None:
    """3584 prompt tokens at a 90% share floor to a 3216-token prefix; 64 prefixes of it."""
    config = load_kv_ab_config(_COMMITTED_CONFIG)

    assert config.prefix_len == 3216
    assert config.working_set_tokens == 64 * 3216


def _write_config(tmp_path: Path, **overrides: object) -> Path:
    """Write the committed config with top-level or ``load.`` keys overridden."""
    data = yaml.safe_load(_COMMITTED_CONFIG.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        section, _, field = key.rpartition("__")
        (data[section] if section else data)[field] = value
    path = tmp_path / "kv-ab.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("align_blocks", [0, 10, 24])
def test_a_prefix_off_the_kv_block_grid_is_rejected(
    tmp_path: Path, align_blocks: int
) -> None:
    """A prefix not a whole number of 16-token blocks leaves an uncached tail."""
    path = _write_config(tmp_path, load__align_blocks=align_blocks)

    with pytest.raises(KvAbConfigError, match="multiple of vLLM's 16-token KV block"):
        load_kv_ab_config(path)


def test_a_zero_prefix_share_is_rejected(tmp_path: Path) -> None:
    """A 0% share has no prefix to reuse, so neither arm has a working set to cache."""
    path = _write_config(tmp_path, prefix_share=0)

    with pytest.raises(KvAbConfigError, match="prefix_share"):
        load_kv_ab_config(path)


def test_a_prefix_the_alignment_floors_to_nothing_is_rejected(tmp_path: Path) -> None:
    """90% of 16 tokens is a 14-token prefix, which no whole 16-token block holds."""
    path = _write_config(tmp_path, load__total_len=16)

    with pytest.raises(KvAbConfigError, match="floors to no whole 16-token block"):
        load_kv_ab_config(path)


def test_an_unknown_key_is_rejected(tmp_path: Path) -> None:
    """A misspelt knob fails the load instead of silently running the default."""
    path = _write_config(tmp_path, run_per_arm=3)

    with pytest.raises(KvAbConfigError, match="run_per_arm"):
        load_kv_ab_config(path)


def test_a_missing_config_is_named(tmp_path: Path) -> None:
    """The error names the path the flow was pointed at."""
    with pytest.raises(KvAbConfigError, match="not found"):
        load_kv_ab_config(tmp_path / "absent.yaml")


def test_an_fp16_kv_cache_is_rejected(tmp_path: Path) -> None:
    """The CPU tier is sized at fp8's 73,728 bytes a token; fp16 holds half as many."""
    path = _write_config(tmp_path, engine__kv_cache_dtype="fp16")

    with pytest.raises(KvAbConfigError, match="kv_cache_dtype"):
        load_kv_ab_config(path)


def test_a_warmup_above_the_lowest_rung_is_rejected(tmp_path: Path) -> None:
    """Warmup fills the tiers at low concurrency: 16 is past the ladder's first rung, 8."""
    path = _write_config(tmp_path, warmup_concurrency=16)

    with pytest.raises(KvAbConfigError, match="warmup_concurrency 16"):
        load_kv_ab_config(path)


def test_a_share_that_floors_to_no_prefix_token_is_rejected(tmp_path: Path) -> None:
    """5% of 16 tokens floors to an empty prefix before any block alignment."""
    path = _write_config(tmp_path, prefix_share=5, load__total_len=16)

    with pytest.raises(KvAbConfigError, match="floors to no whole 16-token block"):
        load_kv_ab_config(path)


def test_a_warmup_at_the_lowest_rung_loads(tmp_path: Path) -> None:
    """A warmup at the ladder's first rung, 8, is still low concurrency."""
    path = _write_config(tmp_path, warmup_concurrency=8)

    assert load_kv_ab_config(path).warmup_concurrency == 8


def test_a_config_that_is_not_yaml_is_named_as_the_kv_ab_config(tmp_path: Path) -> None:
    """The parse error says which config failed, not only its path."""
    path = tmp_path / "kv-ab.yaml"
    path.write_text("engine: [unclosed", encoding="utf-8")

    with pytest.raises(KvAbConfigError, match="kv-ab config .* is not valid YAML"):
        load_kv_ab_config(path)
