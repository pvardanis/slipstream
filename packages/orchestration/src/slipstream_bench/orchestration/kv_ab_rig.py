"""The kv-cache-extension-ab rig's capacities, and the guards sizing the workload to them.

The A/B compares a GPU-only prefix cache with one LMCache extends into a pinned CPU tier,
so its workload is sized against two capacities of the rig: the CPU tier and the context
window. Both are configured in ``k8s/vllm-gpu.yaml`` — the tier by the vLLM container's
``LMCACHE_MAX_LOCAL_CPU_SIZE`` env, the window by its ``--max-model-len`` arg — so they are
read from there, their single source: a rig change is a manifest edit, not a flow edit.
The guards refuse a workload the rig cannot run or cannot measure offload on, before the
cells it would waste. The GPU KV pool is the third capacity: vLLM sizes it at startup, so
it is checked against the size the deployed engine reports, after each redeploy.
"""

import math
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from slipstream_bench.contract.kv_ab_config import KvAbConfig
from slipstream_bench.orchestration.digest import read_vllm_args, read_vllm_env

# The KV bytes one token occupies for the served Qwen3-8B with the fp8 KV cache: 36 layers
# x 8 KV heads x 128 head dim x 2 (K and V) x 1 byte. It sizes the CPU tier in tokens.
_KV_BYTES_PER_TOKEN = 73_728

# LMCache v0.5.4 reads this env as GiB: it reserves int(size * 1024**3) bytes of pinned
# host memory (lmcache/v1/storage_backend/local_cpu_backend.py).
_TIER_SIZE_ENV = "LMCACHE_MAX_LOCAL_CPU_SIZE"
_BYTES_PER_GIB = 1024**3

_MAX_MODEL_LEN_FLAG = "--max-model-len"


class KvAbRigError(Exception):
    """A vLLM manifest that cannot yield the rig's CPU tier size or max-model-len."""


class KvAbWorkloadError(Exception):
    """A workload the rig cannot run, or cannot measure KV cache offload on."""


@dataclass(frozen=True)
class KvAbRig:
    """The rig capacities the A/B workload is sized against.

    :param max_model_len: the tokens one request may span, prompt plus output.
    :param cpu_tier_bytes: the pinned host memory LMCache holds KV chunks in.
    """

    max_model_len: int
    cpu_tier_bytes: int

    def __post_init__(self) -> None:
        """Reject a rig with no window or no tier to size a workload against.

        :raise ValueError: when either capacity is not positive.
        """
        if self.max_model_len <= 0 or self.cpu_tier_bytes <= 0:
            raise ValueError(
                f"KvAbRig capacities must be positive: max_model_len "
                f"{self.max_model_len}, cpu_tier_bytes {self.cpu_tier_bytes}"
            )

    @property
    def cpu_tier_tokens(self) -> int:
        """The tokens of KV the CPU tier holds."""
        return self.cpu_tier_bytes // _KV_BYTES_PER_TOKEN


def read_kv_ab_rig(manifest: Path) -> KvAbRig:
    """Read the rig's max-model-len and CPU tier size off the vLLM manifest.

    :param manifest: the ``k8s/vllm-gpu.yaml`` Deployment manifest.
    :return: the rig's capacities.
    :raise KvAbRigError: when the vLLM container carries no positive ``--max-model-len``
        or ``LMCACHE_MAX_LOCAL_CPU_SIZE``, or a tier size that is not a number.
    :raise DigestError: when the manifest itself cannot be read, or its vLLM container's
        env is not a list (see ``read_vllm_args`` and ``read_vllm_env``).
    """
    return KvAbRig(
        max_model_len=_read_max_model_len(manifest),
        cpu_tier_bytes=_read_cpu_tier_bytes(manifest),
    )


def _read_max_model_len(manifest: Path) -> int:
    """Read the value following the vLLM container's last ``--max-model-len`` arg."""
    args = read_vllm_args(manifest)
    # vLLM's argparse keeps a repeated flag's last value, so the last one is the window.
    value = next(
        (
            after
            for flag, after in reversed(list(pairwise(args)))
            if flag == _MAX_MODEL_LEN_FLAG
        ),
        None,
    )
    is_count = value is not None and value.isascii() and value.isdigit()
    if not is_count or int(value) == 0:
        raise KvAbRigError(
            f"{manifest} vllm container sets no positive {_MAX_MODEL_LEN_FLAG} "
            f"(got {value!r})"
        )
    return int(value)


def _read_cpu_tier_bytes(manifest: Path) -> int:
    """Read the vLLM container's LMCache CPU tier size, in bytes."""
    value = read_vllm_env(manifest).get(_TIER_SIZE_ENV)
    try:
        gib = float(value) if value is not None else 0.0
    except ValueError as error:
        raise KvAbRigError(
            f"{manifest} vllm container's {_TIER_SIZE_ENV} {value!r} is not a number "
            f"of GiB"
        ) from error
    # LMCache reserves int(size * 1024**3) bytes, so a size under one byte reserves none.
    if not (math.isfinite(gib) and int(gib * _BYTES_PER_GIB) > 0):
        raise KvAbRigError(
            f"{manifest} vllm container sets no positive {_TIER_SIZE_ENV} as a literal "
            f"value (got {value!r}); an entry sourced through valueFrom reads as absent"
        )
    return int(gib * _BYTES_PER_GIB)


def check_fits_rig(config: KvAbConfig, rig: KvAbRig) -> None:
    """Refuse a workload the rig's manifest already rules out, before any redeploy.

    Both capacities are read off the manifest, so a workload past either fails before a GPU
    minute is spent: a request past max-model-len is rejected by vLLM, so every cell would
    fail alike; and a working set past the CPU tier is evicted from it before it is
    served, so both arms would measure recompute.

    :param config: the A/B workload.
    :param rig: the rig capacities read off the manifest.
    :raise KvAbWorkloadError: when prompt plus output exceeds max-model-len, or the
        working set overflows the CPU tier.
    """
    request_len = config.load.total_len + config.load.output_len
    if request_len > rig.max_model_len:
        raise KvAbWorkloadError(
            f"a request spans {request_len} tokens ({config.load.total_len} prompt + "
            f"{config.load.output_len} output), past the rig's max-model-len "
            f"{rig.max_model_len}: lower load.total_len or load.output_len"
        )
    working_set = config.working_set_tokens
    if working_set > rig.cpu_tier_tokens:
        raise KvAbWorkloadError(
            f"the {working_set}-token working set overflows the "
            f"{rig.cpu_tier_tokens}-token CPU tier, so the tier evicts what it would "
            f"serve: raise {_TIER_SIZE_ENV} in the manifest or shrink the working set"
        )


def check_overflows_gpu_pool(config: KvAbConfig, *, gpu_pool_tokens: int) -> None:
    """Refuse a working set the GPU KV pool holds whole, once the engine has sized it.

    A pool that holds every prefix never evicts one, so the GPU prefix cache alone serves
    the workload and the CPU tier has nothing to add. vLLM sizes the pool by profiling
    memory at startup, so this runs after each redeploy, on the size the engine reported.

    :param config: the A/B workload.
    :param gpu_pool_tokens: the GPU KV pool size the deployed engine reported.
    :raise KvAbWorkloadError: when the working set fits the GPU pool.
    """
    working_set = config.working_set_tokens
    if working_set <= gpu_pool_tokens:
        raise KvAbWorkloadError(
            f"the {working_set}-token working set fits the {gpu_pool_tokens}-token "
            f"GPU KV pool, so offload has nothing to serve: raise load.num_prefixes "
            f"or prefix_share"
        )
