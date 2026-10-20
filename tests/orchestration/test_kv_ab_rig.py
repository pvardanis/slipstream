"""Tests for the kv-cache-extension-ab rig: its capacities and the workload guards.

The rig's CPU tier size, its chunk size, and max-model-len are read off the vLLM manifest
that configures them, their single source. The guards hold each request within
max-model-len, each prefix to whole LMCache chunks, the prompts' chunks within the CPU
tier, and the working set above a GPU KV pool that still holds the top rung's requests,
so the A/B measures offload rather than a cache that never fills, a tier that cannot hold
the workload, or preemption.
"""

from pathlib import Path

import pytest
import yaml

from slipstream_bench.orchestration.digest import DigestError
from slipstream_bench.orchestration.kv_ab_config import KvAbConfig, load_kv_ab_config
from slipstream_bench.orchestration.kv_ab_rig import (
    KvAbRig,
    KvAbRigError,
    KvAbWorkloadError,
    check_fits_rig,
    check_gpu_kv_pool,
    read_kv_ab_rig,
)

_COMMITTED_MANIFEST = Path(__file__).parents[2] / "k8s" / "vllm-gpu.yaml"


def test_the_committed_manifest_yields_the_rig_capacities() -> None:
    """A 4.0 GiB tier is 4,294,967,296 bytes: 58,254 fp8 tokens at 73,728 bytes each."""
    rig = read_kv_ab_rig(_COMMITTED_MANIFEST)

    assert rig.max_model_len == 4096
    assert rig.cpu_tier_tokens == 58_254
    assert rig.chunk_tokens == 256


def _write_manifest(tmp_path: Path, *, args: list[str], env: dict[str, str]) -> Path:
    """Write a minimal Deployment whose vllm container carries the args and env."""
    container = {
        "name": "vllm",
        "image": "vllm/vllm-openai:v0.29.0",
        "args": args,
        "env": [{"name": name, "value": value} for name, value in env.items()],
    }
    document = {"spec": {"template": {"spec": {"containers": [container]}}}}
    path = tmp_path / "vllm-gpu.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


_ARGS = ["--max-model-len", "4096"]
_ENV = {"LMCACHE_MAX_LOCAL_CPU_SIZE": "4.0", "LMCACHE_CHUNK_SIZE": "256"}


@pytest.mark.parametrize(
    "args", [["--model", "x"], ["--max-model-len"], ["--max-model-len", "0"]]
)
def test_a_manifest_without_a_positive_max_model_len_is_rejected(
    tmp_path: Path, args: list[str]
) -> None:
    """No flag, a flag with no value, or a zero window cannot size a prompt."""
    manifest = _write_manifest(tmp_path, args=args, env=_ENV)

    with pytest.raises(KvAbRigError, match="--max-model-len"):
        read_kv_ab_rig(manifest)


@pytest.mark.parametrize("size", [None, "", "four", "0", "-1.0", "nan", "inf", "1e-12"])
def test_a_manifest_without_a_positive_tier_size_is_rejected(
    tmp_path: Path, size: str | None
) -> None:
    """An absent, unparseable, empty, or non-finite tier size cannot size the tier."""
    env = {"LMCACHE_CHUNK_SIZE": "256"}
    if size is not None:
        env["LMCACHE_MAX_LOCAL_CPU_SIZE"] = size
    manifest = _write_manifest(tmp_path, args=_ARGS, env=env)

    with pytest.raises(KvAbRigError, match="LMCACHE_MAX_LOCAL_CPU_SIZE"):
        read_kv_ab_rig(manifest)


def test_a_fractional_tier_size_is_read_as_gib(tmp_path: Path) -> None:
    """LMCache reserves int(size * 1024**3) bytes: 0.5 is 536,870,912 bytes."""
    env = {**_ENV, "LMCACHE_MAX_LOCAL_CPU_SIZE": "0.5"}
    manifest = _write_manifest(tmp_path, args=_ARGS, env=env)

    assert read_kv_ab_rig(manifest).cpu_tier_bytes == 536_870_912


@pytest.mark.parametrize("size", [None, "", "0", "256.0", "²"])
def test_a_manifest_without_a_positive_chunk_size_is_rejected(
    tmp_path: Path, size: str | None
) -> None:
    """An absent, empty, zero, or non-integer chunk size cannot align a prefix."""
    env = {"LMCACHE_MAX_LOCAL_CPU_SIZE": "4.0"}
    if size is not None:
        env["LMCACHE_CHUNK_SIZE"] = size
    manifest = _write_manifest(tmp_path, args=_ARGS, env=env)

    with pytest.raises(KvAbRigError, match="LMCACHE_CHUNK_SIZE"):
        read_kv_ab_rig(manifest)


_BENCH = _COMMITTED_MANIFEST.parents[1] / "bench"
_AGENT = load_kv_ab_config(_BENCH / "kv-cache-extension-ab-agent.yaml")
_CHAT = load_kv_ab_config(_BENCH / "kv-cache-extension-ab-chat.yaml")
_KV_BYTES_PER_TOKEN = 73_728
# The committed manifest's rig: a 4096-token window, a 4.0 GiB tier, 256-token chunks.
_RIG = KvAbRig(max_model_len=4096, cpu_tier_bytes=4 * 1024**3, chunk_tokens=256)


def _with_load(config: KvAbConfig, **load: object) -> KvAbConfig:
    """Copy the config with some of its load knobs replaced."""
    return config.model_copy(update={"load": config.load.model_copy(update=load)})


def _with_pool(config: KvAbConfig, tokens: int) -> KvAbConfig:
    """Copy the config with a GPU KV pool of ``tokens`` fp8 tokens."""
    engine = config.engine.model_copy(
        update={"kv_cache_memory_bytes": tokens * _KV_BYTES_PER_TOKEN}
    )
    return config.model_copy(update={"engine": engine})


@pytest.mark.parametrize("config", [_AGENT, _CHAT], ids=["agent", "chat"])
def test_the_committed_workloads_fit_the_committed_manifest(config: KvAbConfig) -> None:
    """Both workloads were sized against the g5.xlarge manifest's window, tier, and chunk."""
    check_fits_rig(config, read_kv_ab_rig(_COMMITTED_MANIFEST))


def test_a_request_past_max_model_len_is_refused() -> None:
    """3584 prompt plus 128 output tokens is 3712, past a 3700-token window."""
    rig = KvAbRig(max_model_len=3700, cpu_tier_bytes=4 * 1024**3, chunk_tokens=256)

    with pytest.raises(KvAbWorkloadError, match="max-model-len"):
        check_fits_rig(_AGENT, rig)


def test_a_request_exactly_filling_max_model_len_fits_the_rig() -> None:
    """3584 prompt plus 128 output tokens is 3712, the whole of a 3712-token window."""
    rig = KvAbRig(max_model_len=3712, cpu_tier_bytes=4 * 1024**3, chunk_tokens=256)

    check_fits_rig(_AGENT, rig)


def test_a_prefix_off_the_lmcache_chunk_grid_is_refused() -> None:
    """A 3216-token prefix ends 144 tokens into its 13th chunk, which no prompt reuses."""
    config = _with_load(_AGENT, align_blocks=16)

    with pytest.raises(KvAbWorkloadError, match="256-token LMCache chunk"):
        check_fits_rig(config, _RIG)


def test_prompts_that_exactly_fill_the_cpu_tier_fit_the_rig() -> None:
    """8 prefixes' prompts of 3584 tokens, 14 whole chunks each, fill 28,672 tokens."""
    rig = KvAbRig(
        max_model_len=4096,
        cpu_tier_bytes=28_672 * _KV_BYTES_PER_TOKEN,
        chunk_tokens=256,
    )

    check_fits_rig(_AGENT, rig)


def test_prompts_past_the_cpu_tier_are_refused() -> None:
    """The unique tails share the tier with the prefixes, so one token short refuses."""
    rig = KvAbRig(
        max_model_len=4096,
        cpu_tier_bytes=28_671 * _KV_BYTES_PER_TOKEN,
        chunk_tokens=256,
    )

    with pytest.raises(KvAbWorkloadError, match="28672 tokens of prompt chunks"):
        check_fits_rig(_AGENT, rig)


def test_a_partial_last_chunk_counts_whole_against_the_cpu_tier() -> None:
    """3600 tokens span 15 chunks: 8 prompts hold 30,720 tokens of tier, not 28,800."""
    config = _with_load(_AGENT, total_len=3600)
    rig = KvAbRig(
        max_model_len=4096,
        cpu_tier_bytes=30_719 * _KV_BYTES_PER_TOKEN,
        chunk_tokens=256,
    )

    with pytest.raises(KvAbWorkloadError, match="30720 tokens of prompt chunks"):
        check_fits_rig(config, rig)


def test_a_configured_pool_that_holds_the_working_set_is_refused_before_deploy() -> (
    None
):
    """A 24,576-token pool holds all 8 agent prefixes, so nothing would be evicted."""
    with pytest.raises(KvAbWorkloadError, match="24576-token working set fits"):
        check_fits_rig(_with_pool(_AGENT, 24_576), _RIG)


def test_a_configured_pool_below_the_top_rung_is_refused_before_deploy() -> None:
    """4 requests of 3712 tokens need 14,848 tokens; 14,847 would preempt one."""
    with pytest.raises(KvAbWorkloadError, match="top rung"):
        check_fits_rig(_with_pool(_AGENT, 14_847), _RIG)


@pytest.mark.parametrize("gpu_pool_tokens", [14_848, 16_384, 24_575])
def test_a_scraped_pool_between_the_top_rung_and_the_working_set_passes(
    gpu_pool_tokens: int,
) -> None:
    """The pool holds 4 x 3712 tokens and is overflowed by the 24,576-token set."""
    check_gpu_kv_pool(_AGENT, gpu_pool_tokens=gpu_pool_tokens)


@pytest.mark.parametrize("gpu_pool_tokens", [24_576, 185_696])
def test_a_scraped_pool_that_holds_the_working_set_is_refused(
    gpu_pool_tokens: int,
) -> None:
    """A pool that holds every prefix never evicts, so offload has nothing to serve."""
    with pytest.raises(KvAbWorkloadError, match="24576-token working set fits"):
        check_gpu_kv_pool(_AGENT, gpu_pool_tokens=gpu_pool_tokens)


def test_a_scraped_pool_below_the_top_rung_is_refused() -> None:
    """The engine sized the pool below 4 x 3712 tokens, so the top rung would preempt."""
    with pytest.raises(KvAbWorkloadError, match="top rung"):
        check_gpu_kv_pool(_AGENT, gpu_pool_tokens=14_847)


def test_a_configured_pool_a_byte_short_of_the_top_rung_is_refused() -> None:
    """One byte under 14,848 tokens of KV floors to 14,847 whole tokens: a preempt."""
    engine = _AGENT.engine.model_copy(
        update={"kv_cache_memory_bytes": 14_848 * _KV_BYTES_PER_TOKEN - 1}
    )

    with pytest.raises(KvAbWorkloadError, match="top rung"):
        check_fits_rig(_AGENT.model_copy(update={"engine": engine}), _RIG)


def test_the_top_rung_is_the_ladder_s_highest_whatever_its_order() -> None:
    """A [4, 1, 2] ladder still runs 4 requests of 3712 tokens at once."""
    config = _AGENT.model_copy(update={"max_concurrency": [4, 1, 2]})

    with pytest.raises(KvAbWorkloadError, match="top rung's 4 requests"):
        check_gpu_kv_pool(config, gpu_pool_tokens=14_847)


@pytest.mark.parametrize("env", ["LMCACHE_MAX_LOCAL_CPU_SIZE=4.0", {"a": "b"}])
def test_a_vllm_env_that_is_not_a_list_is_rejected(tmp_path: Path, env: object) -> None:
    """A malformed env block is named, not read as a container without a tier."""
    manifest = _write_manifest(tmp_path, args=_ARGS, env={})
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["spec"]["template"]["spec"]["containers"][0]["env"] = env
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(DigestError, match="env is not a list"):
        read_kv_ab_rig(manifest)


def test_the_last_max_model_len_flag_wins_as_in_vllm(tmp_path: Path) -> None:
    """vLLM's argparse keeps a repeated flag's last value, so the reader does too."""
    args = ["--max-model-len", "2048", "--max-model-len", "4096"]
    manifest = _write_manifest(tmp_path, args=args, env=_ENV)

    assert read_kv_ab_rig(manifest).max_model_len == 4096


def test_a_non_ascii_digit_max_model_len_is_rejected(tmp_path: Path) -> None:
    """A superscript digit passes str.isdigit but is no window int() can read."""
    manifest = _write_manifest(tmp_path, args=["--max-model-len", "²"], env=_ENV)

    with pytest.raises(KvAbRigError, match="--max-model-len"):
        read_kv_ab_rig(manifest)


def test_a_tier_size_that_is_not_a_number_is_named_as_such(tmp_path: Path) -> None:
    """An unparseable size is told apart from an absent or non-positive one."""
    env = {"LMCACHE_MAX_LOCAL_CPU_SIZE": "4GiB"}
    manifest = _write_manifest(tmp_path, args=_ARGS, env=env)

    with pytest.raises(KvAbRigError, match="'4GiB' is not a number of GiB"):
        read_kv_ab_rig(manifest)


def test_a_tier_size_sourced_through_value_from_is_rejected(tmp_path: Path) -> None:
    """A valueFrom entry has no size in the manifest, so the error asks for a literal."""
    manifest = _write_manifest(tmp_path, args=_ARGS, env={})
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {
            "name": "LMCACHE_MAX_LOCAL_CPU_SIZE",
            "valueFrom": {"configMapKeyRef": {"name": "lmcache", "key": "size"}},
        },
        {"name": "LMCACHE_CHUNK_SIZE", "value": "256"},
    ]
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(KvAbRigError, match="literal value"):
        read_kv_ab_rig(manifest)


@pytest.mark.parametrize(
    ("max_model_len", "cpu_tier_bytes", "chunk_tokens"),
    [(0, 1024**3, 256), (4096, 0, 256), (4096, -1, 256), (4096, 1024**3, 0)],
)
def test_a_rig_without_positive_capacities_is_rejected(
    max_model_len: int, cpu_tier_bytes: int, chunk_tokens: int
) -> None:
    """A zero window, an empty tier, or a zero chunk is no rig to size a workload against."""
    with pytest.raises(ValueError, match="positive"):
        KvAbRig(
            max_model_len=max_model_len,
            cpu_tier_bytes=cpu_tier_bytes,
            chunk_tokens=chunk_tokens,
        )
