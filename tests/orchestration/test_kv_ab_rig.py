"""Tests for the kv-cache-extension-ab rig: its capacities and the workload guards.

The rig's CPU tier size and max-model-len are read off the vLLM manifest that configures
them, its single source; the guards hold each request within max-model-len and the
workload's working set above the GPU KV pool and within the CPU tier, so the A/B
measures offload rather than a cache that never fills or a tier that cannot hold the
workload.
"""

from pathlib import Path

import pytest
import yaml

from slipstream_bench.contract.kv_ab_config import load_kv_ab_config
from slipstream_bench.orchestration.digest import DigestError
from slipstream_bench.orchestration.kv_ab_rig import (
    KvAbRig,
    KvAbRigError,
    KvAbWorkloadError,
    check_fits_rig,
    check_overflows_gpu_pool,
    read_kv_ab_rig,
)

_COMMITTED_MANIFEST = Path(__file__).parents[2] / "k8s" / "vllm-gpu.yaml"


def test_the_committed_manifest_yields_the_rig_capacities() -> None:
    """A 4.0 GiB tier is 4,294,967,296 bytes: 58,254 fp8 tokens at 73,728 bytes each."""
    rig = read_kv_ab_rig(_COMMITTED_MANIFEST)

    assert rig.max_model_len == 4096
    assert rig.cpu_tier_tokens == 58_254


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
_ENV = {"LMCACHE_MAX_LOCAL_CPU_SIZE": "4.0"}


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


@pytest.mark.parametrize("size", [None, "", "four", "0", "-1.0", "nan", "inf"])
def test_a_manifest_without_a_positive_tier_size_is_rejected(
    tmp_path: Path, size: str | None
) -> None:
    """An absent, unparseable, empty, or non-finite tier size cannot size the tier."""
    env = {} if size is None else {"LMCACHE_MAX_LOCAL_CPU_SIZE": size}
    manifest = _write_manifest(tmp_path, args=_ARGS, env=env)

    with pytest.raises(KvAbRigError, match="LMCACHE_MAX_LOCAL_CPU_SIZE"):
        read_kv_ab_rig(manifest)


def test_a_fractional_tier_size_is_read_as_gib(tmp_path: Path) -> None:
    """LMCache reserves int(size * 1024**3) bytes: 0.5 is 536,870,912 bytes."""
    manifest = _write_manifest(
        tmp_path, args=_ARGS, env={"LMCACHE_MAX_LOCAL_CPU_SIZE": "0.5"}
    )

    assert read_kv_ab_rig(manifest).cpu_tier_bytes == 536_870_912


_CONFIG = load_kv_ab_config(
    _COMMITTED_MANIFEST.parents[1] / "bench" / "kv-cache-extension-ab.yaml"
)
# 16 GiB holds 233,016 fp8 tokens: room for the committed 205,824-token working set.
_ROOMY_RIG = KvAbRig(max_model_len=4096, cpu_tier_bytes=16 * 1024**3)
_G5_XLARGE_POOL = 185_696


def test_a_workload_within_the_window_and_the_tier_fits_the_rig() -> None:
    """3712-token requests fit 4096; 205,824 working-set tokens fit 233,016 in the tier."""
    check_fits_rig(_CONFIG, _ROOMY_RIG)


def test_a_request_past_max_model_len_is_refused() -> None:
    """3584 prompt plus 128 output tokens is 3712, past a 3700-token window."""
    rig = KvAbRig(max_model_len=3700, cpu_tier_bytes=16 * 1024**3)

    with pytest.raises(KvAbWorkloadError, match="max-model-len"):
        check_fits_rig(_CONFIG, rig)


def test_a_working_set_past_the_cpu_tier_is_refused() -> None:
    """The 4 GiB tier holds 58,254 tokens, a fraction of the 205,824-token set."""
    rig = KvAbRig(max_model_len=4096, cpu_tier_bytes=4 * 1024**3)

    with pytest.raises(KvAbWorkloadError, match="CPU tier"):
        check_fits_rig(_CONFIG, rig)


def test_a_working_set_that_exactly_fills_the_tier_fits_the_rig() -> None:
    """205,824 tokens x 73,728 bytes is a tier the working set fills to the token."""
    rig = KvAbRig(max_model_len=4096, cpu_tier_bytes=205_824 * 73_728)

    check_fits_rig(_CONFIG, rig)


def test_a_working_set_past_the_gpu_pool_passes() -> None:
    """205,824 tokens overflow the g5.xlarge's 185,696-token pool."""
    check_overflows_gpu_pool(_CONFIG, gpu_pool_tokens=_G5_XLARGE_POOL)


@pytest.mark.parametrize("gpu_pool_tokens", [205_824, 300_000])
def test_a_working_set_the_gpu_pool_holds_is_refused(gpu_pool_tokens: int) -> None:
    """A pool that holds every prefix never evicts, so offload has nothing to serve."""
    with pytest.raises(KvAbWorkloadError, match="GPU KV pool"):
        check_overflows_gpu_pool(_CONFIG, gpu_pool_tokens=gpu_pool_tokens)


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
        }
    ]
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(KvAbRigError, match="literal value"):
        read_kv_ab_rig(manifest)


def test_a_request_exactly_filling_max_model_len_fits_the_rig() -> None:
    """3584 prompt plus 128 output tokens is 3712, the whole of a 3712-token window."""
    check_fits_rig(_CONFIG, KvAbRig(max_model_len=3712, cpu_tier_bytes=16 * 1024**3))


def test_the_committed_manifest_tier_is_refused_for_the_committed_workload() -> None:
    """The g5.xlarge manifest's 4 GiB tier holds 58,254 of the 205,824 tokens."""
    rig = read_kv_ab_rig(_COMMITTED_MANIFEST)

    with pytest.raises(KvAbWorkloadError, match="58254-token CPU tier"):
        check_fits_rig(_CONFIG, rig)


@pytest.mark.parametrize(
    ("max_model_len", "cpu_tier_bytes"), [(0, 1024**3), (4096, 0), (4096, -1)]
)
def test_a_rig_without_positive_capacities_is_rejected(
    max_model_len: int, cpu_tier_bytes: int
) -> None:
    """A zero window or an empty tier is no rig to size a workload against."""
    with pytest.raises(ValueError, match="positive"):
        KvAbRig(max_model_len=max_model_len, cpu_tier_bytes=cpu_tier_bytes)
