"""The kv-cache-extension-ab rig's capacities, and the guards sizing the workload to them.

The A/B compares a GPU-only prefix cache with one LMCache extends into a pinned CPU tier,
so its workload is sized against the rig's CPU tier, the tier's chunk size, and the
context window. All three are configured in ``k8s/vllm-gpu.yaml`` — the tier and chunk by
the vLLM container's ``LMCACHE_MAX_LOCAL_CPU_SIZE`` and ``LMCACHE_CHUNK_SIZE`` env, the
window by its ``--max-model-len`` arg — so they are read from there, their single source:
a rig change is a manifest edit, not a flow edit.
The guards refuse a workload the rig cannot run or cannot measure offload on, before the
cells it would waste. The GPU KV pool is checked twice: against the config's
``engine.kv_cache_memory_bytes`` before the first redeploy, and against the size the
deployed engine reports after each.
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

# LMCache stores and reuses KV in chunks of this many tokens; a chunk is reused only when
# every token in it matches, so a prefix ending mid-chunk loses that chunk's reuse.
_CHUNK_SIZE_ENV = "LMCACHE_CHUNK_SIZE"

_MAX_MODEL_LEN_FLAG = "--max-model-len"


class KvAbRigError(Exception):
    """A vLLM manifest that cannot yield the rig's CPU tier, chunk, or max-model-len."""


class KvAbWorkloadError(Exception):
    """A workload the rig cannot run, or cannot measure KV cache offload on."""


@dataclass(frozen=True)
class KvAbRig:
    """The rig capacities the A/B workload is sized against.

    :param max_model_len: the tokens one request may span, prompt plus output.
    :param cpu_tier_bytes: the pinned host memory LMCache holds KV chunks in.
    :param chunk_tokens: the tokens of one LMCache chunk.
    """

    max_model_len: int
    cpu_tier_bytes: int
    chunk_tokens: int

    def __post_init__(self) -> None:
        """Reject a rig with no window, tier, or chunk to size a workload against.

        :raise ValueError: when any capacity is not positive.
        """
        if min(self.max_model_len, self.cpu_tier_bytes, self.chunk_tokens) <= 0:
            raise ValueError(
                f"KvAbRig capacities must be positive: max_model_len "
                f"{self.max_model_len}, cpu_tier_bytes {self.cpu_tier_bytes}, "
                f"chunk_tokens {self.chunk_tokens}"
            )

    @property
    def cpu_tier_tokens(self) -> int:
        """The tokens of KV the CPU tier holds."""
        return self.cpu_tier_bytes // _KV_BYTES_PER_TOKEN


def read_kv_ab_rig(manifest: Path) -> KvAbRig:
    """Read the rig's max-model-len, CPU tier size, and chunk size off the vLLM manifest.

    :param manifest: the ``k8s/vllm-gpu.yaml`` Deployment manifest.
    :return: the rig's capacities.
    :raise KvAbRigError: when the vLLM container carries no positive ``--max-model-len``,
        ``LMCACHE_MAX_LOCAL_CPU_SIZE``, or ``LMCACHE_CHUNK_SIZE``, or a tier size that is
        not a number.
    :raise DigestError: when the manifest itself cannot be read, or its vLLM container's
        env is not a list (see ``read_vllm_args`` and ``read_vllm_env``).
    """
    return KvAbRig(
        max_model_len=_read_max_model_len(manifest),
        cpu_tier_bytes=_read_cpu_tier_bytes(manifest),
        chunk_tokens=_read_chunk_tokens(manifest),
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


def _read_chunk_tokens(manifest: Path) -> int:
    """Read the vLLM container's LMCache chunk size, in tokens."""
    value = read_vllm_env(manifest).get(_CHUNK_SIZE_ENV)
    if value is None or not (value.isascii() and value.isdigit()) or int(value) == 0:
        raise KvAbRigError(
            f"{manifest} vllm container sets no positive whole {_CHUNK_SIZE_ENV} as a "
            f"literal value (got {value!r})"
        )
    return int(value)


def check_fits_rig(config: KvAbConfig, rig: KvAbRig) -> None:
    """Refuse a workload the manifest or its own engine config rules out, before deploy.

    The window, tier, and chunk are read off the manifest and the GPU KV pool is set by
    the config's ``kv_cache_memory_bytes``, so a workload past any fails before a GPU
    minute is spent: a request past max-model-len is rejected by vLLM, so every cell
    would fail alike; a prefix ending mid-chunk loses that chunk's reuse from the tier;
    prompts whose chunks overflow the tier evict the prefixes it would serve; a pool that
    holds the working set measures no offload; and one below the top rung's requests
    measures preemption in both arms.

    :param config: the A/B workload.
    :param rig: the rig capacities read off the manifest.
    :raise KvAbWorkloadError: when prompt plus output exceeds max-model-len, the prefix
        is not whole chunks, the prompts' chunks overflow the CPU tier, or the configured
        GPU KV pool holds the working set or not the top rung's requests.
    """
    request_len = config.request_tokens
    if request_len > rig.max_model_len:
        raise KvAbWorkloadError(
            f"a request spans {request_len} tokens ({config.load.total_len} prompt + "
            f"{config.load.output_len} output), past the rig's max-model-len "
            f"{rig.max_model_len}: lower load.total_len or load.output_len"
        )
    _check_fits_cpu_tier(config, rig)
    check_gpu_pool(
        config,
        gpu_pool_tokens=config.engine.kv_cache_memory_bytes // _KV_BYTES_PER_TOKEN,
    )


def _check_fits_cpu_tier(config: KvAbConfig, rig: KvAbRig) -> None:
    """Refuse a prefix off the chunk grid, or prompts whose chunks overflow the tier.

    LMCache stores every chunk of every prompt, so between two reuses of a prefix the
    tier also takes the other prompts' unique tails: it must hold each prefix's whole
    prompt, rounded up to whole chunks, not only the prefixes.
    """
    chunk = rig.chunk_tokens
    if config.prefix_len % chunk:
        raise KvAbWorkloadError(
            f"the {config.prefix_len}-token prefix is not a whole number of "
            f"{chunk}-token LMCache chunks, so its last chunk is never reused: set "
            f"load.align_blocks to a multiple of {chunk}"
        )
    prompt_chunk_tokens = math.ceil(config.load.total_len / chunk) * chunk
    tier_demand = config.load.num_prefixes * prompt_chunk_tokens
    if tier_demand > rig.cpu_tier_tokens:
        raise KvAbWorkloadError(
            f"{config.load.num_prefixes} prefixes' prompts need {tier_demand} tokens of "
            f"prompt chunks, past the {rig.cpu_tier_tokens}-token CPU tier, so the tier "
            f"evicts what it would serve: raise {_TIER_SIZE_ENV} in the manifest or "
            f"lower load.num_prefixes"
        )


def check_gpu_pool(config: KvAbConfig, *, gpu_pool_tokens: int) -> None:
    """Refuse a GPU KV pool that holds the working set, or not the top rung's requests.

    A pool that holds every prefix never evicts one, so the GPU prefix cache alone serves
    the workload and the CPU tier has nothing to add; a pool below the top rung's
    requests preempts them, so both arms measure recompute. :func:`check_fits_rig` runs
    this on the configured pool before deploy, and the driver on the size the engine
    reported after each redeploy.

    :param config: the A/B workload.
    :param gpu_pool_tokens: the GPU KV pool size, in tokens.
    :raise KvAbWorkloadError: when the working set fits the pool, or the top rung's
        requests do not.
    """
    working_set = config.working_set_tokens
    if working_set <= gpu_pool_tokens:
        raise KvAbWorkloadError(
            f"the {working_set}-token working set fits the {gpu_pool_tokens}-token "
            f"GPU KV pool, so offload has nothing to serve: raise load.num_prefixes "
            f"or lower engine.kv_cache_memory_bytes"
        )
    top_rung = max(config.max_concurrency)
    request_len = config.request_tokens
    if top_rung * request_len > gpu_pool_tokens:
        raise KvAbWorkloadError(
            f"the top rung's {top_rung} requests of {request_len} tokens need "
            f"{top_rung * request_len} tokens, past the {gpu_pool_tokens}-token GPU KV "
            f"pool, so vLLM preempts them: raise engine.kv_cache_memory_bytes or lower "
            f"max_concurrency"
        )
