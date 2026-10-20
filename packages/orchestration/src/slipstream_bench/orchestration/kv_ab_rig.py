"""The kv-cache-extension-ab rig's capacities, read off the vLLM manifest that sets them.

The A/B compares a GPU-only prefix cache with one LMCache extends into a pinned CPU tier,
so its workload is sized against two capacities of the rig: the CPU tier and the context
window. Both are configured in ``k8s/vllm-gpu.yaml`` — the tier by the vLLM container's
``LMCACHE_MAX_LOCAL_CPU_SIZE`` env, the window by its ``--max-model-len`` arg — so they are
read from there, their single source: a rig change is a manifest edit, not a flow edit.
"""

import math
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

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


@dataclass(frozen=True)
class KvAbRig:
    """The rig capacities the A/B workload is sized against.

    :param max_model_len: the tokens one request may span, prompt plus output.
    :param cpu_tier_bytes: the pinned host memory LMCache holds KV chunks in.
    """

    max_model_len: int
    cpu_tier_bytes: int

    @property
    def cpu_tier_tokens(self) -> int:
        """The tokens of KV the CPU tier holds."""
        return self.cpu_tier_bytes // _KV_BYTES_PER_TOKEN


def read_kv_ab_rig(manifest: Path) -> KvAbRig:
    """Read the rig's max-model-len and CPU tier size off the vLLM manifest.

    :param manifest: the ``k8s/vllm-gpu.yaml`` Deployment manifest.
    :return: the rig's capacities.
    :raise KvAbRigError: when the vLLM container carries no positive ``--max-model-len``
        or ``LMCACHE_MAX_LOCAL_CPU_SIZE``.
    :raise DigestError: when the manifest itself cannot be read (see ``read_vllm_args``).
    """
    return KvAbRig(
        max_model_len=_read_max_model_len(manifest),
        cpu_tier_bytes=_read_cpu_tier_bytes(manifest),
    )


def _read_max_model_len(manifest: Path) -> int:
    """Read the value following the vLLM container's ``--max-model-len`` arg."""
    args = read_vllm_args(manifest)
    value = next(
        (after for flag, after in pairwise(args) if flag == _MAX_MODEL_LEN_FLAG),
        None,
    )
    if value is None or not value.isdigit() or int(value) == 0:
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
    except ValueError:
        gib = 0.0
    if not (math.isfinite(gib) and gib > 0):
        raise KvAbRigError(
            f"{manifest} vllm container sets no positive {_TIER_SIZE_ENV} "
            f"(got {value!r})"
        )
    return int(gib * _BYTES_PER_GIB)
