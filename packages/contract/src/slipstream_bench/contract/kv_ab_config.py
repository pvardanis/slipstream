"""The kv-cache-extension-ab workload config: what both arms of the A/B run.

bench/kv-cache-extension-ab-agent.yaml and bench/kv-cache-extension-ab-chat.yaml each hold
a workload the KV cache offload A/B measures under each arm: the engine knobs held fixed
across both arms, the prefix share, the concurrency ladder, the warmup rung, the runs per
arm, and the client-load knobs the knob sweep's cells share (``LoadKnobs``). Each is
validated on load, so a malformed workload fails before the first GPU redeploy. The rig's
capacities — the CPU tier, its chunk size, and max-model-len — are not here: the
orchestration worker reads them off the vLLM manifest that configures them.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from slipstream_bench.contract.config import LoadKnobs, SweepError
from slipstream_bench.contract.fields import PositiveInt, UniquePositiveInts
from slipstream_bench.contract.naming import split_lengths
from slipstream_bench.contract.yaml_model import load_yaml_model

# vLLM's KV block size: the manifest sets no --block-size, so the engine runs its default.
# Only full blocks are prefix-cached, so a prefix that is not a whole number of blocks
# leaves a recomputed tail that understates the reuse the arms compare.
_VLLM_BLOCK_TOKENS = 16


class KvAbConfigError(Exception):
    """A kv-cache-extension-ab config that cannot be read or does not validate."""


class KvAbEngine(BaseModel):
    """The engine knobs both arms are deployed with; only the offload switch differs.

    Prefix caching is on in both arms: the off arm turns off the LMCache connector alone,
    so the A/B isolates what the CPU tier adds over the GPU prefix cache.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_num_seqs: PositiveInt
    # fp8 only: the guard sizes the CPU tier at fp8's bytes per token, so another dtype
    # would let a working set the tier cannot hold pass it.
    kv_cache_dtype: Literal["fp8"]
    # The GPU KV pool size, which vLLM's --kv-cache-memory-bytes sets in place of the share
    # gpu-memory-utilization leaves after weights and activations.
    kv_cache_memory_bytes: PositiveInt


class KvAbConfig(BaseModel):
    """The whole kv-cache-extension-ab workload, validated on load."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: KvAbEngine
    runs_per_arm: PositiveInt
    # A share of 0 has no prefix to reuse, so no working set for either arm to cache.
    prefix_share: Annotated[int, Field(gt=0, le=100)]
    max_concurrency: UniquePositiveInts
    warmup_concurrency: PositiveInt
    load: LoadKnobs

    @model_validator(mode="after")
    def _require_block_aligned_prefixes(self) -> "KvAbConfig":
        """Hold the prefixes to whole vLLM KV blocks, and to a non-empty prefix.

        :raise ValueError: when ``align_blocks`` is not a positive multiple of the
            block size, or the share and alignment floor the prefix to nothing.
        """
        self._check_align_blocks_on_the_kv_block_grid()
        self._check_prefix_is_not_empty()
        return self

    @model_validator(mode="after")
    def _hold_warmup_to_the_lowest_rung(self) -> "KvAbConfig":
        """Hold the warmup to low concurrency, at or below the ladder's lowest rung.

        :raise ValueError: when the warmup runs above every rung it warms up for.
        """
        lowest = min(self.max_concurrency)
        if self.warmup_concurrency > lowest:
            raise ValueError(
                f"warmup_concurrency {self.warmup_concurrency} is above the ladder's "
                f"lowest rung {lowest}: the warmup fills the tiers at low concurrency"
            )
        return self

    @property
    def prefix_len(self) -> int:
        """The tokens of each shared prefix, floored to whole KV blocks."""
        prefix_len, _ = split_lengths(
            self.load.total_len, self.prefix_share, align_blocks=self.load.align_blocks
        )
        return prefix_len

    @property
    def request_tokens(self) -> int:
        """The tokens one request spans, prompt plus output."""
        return self.load.total_len + self.load.output_len

    @property
    def working_set_tokens(self) -> int:
        """The reusable tokens the workload cycles through: every distinct prefix."""
        return self.load.num_prefixes * self.prefix_len

    def _check_align_blocks_on_the_kv_block_grid(self) -> None:
        """Hold ``align_blocks`` to a positive multiple of vLLM's KV block.

        :raise ValueError: when it is not.
        """
        align = self.load.align_blocks
        if align <= 0 or align % _VLLM_BLOCK_TOKENS:
            raise ValueError(
                f"load.align_blocks {align} must be a positive multiple of vLLM's "
                f"{_VLLM_BLOCK_TOKENS}-token KV block, so every prefix is fully cached"
            )

    def _check_prefix_is_not_empty(self) -> None:
        """Hold the share and alignment to at least one whole block of prefix.

        :raise ValueError: when they floor the prefix to nothing.
        """
        align = self.load.align_blocks
        empty_prefix = ValueError(
            f"prefix_share {self.prefix_share}% of load.total_len "
            f"{self.load.total_len} leaves no whole {align}-token block of prefix: "
            f"raise load.total_len or prefix_share"
        )
        try:
            prefix_len, _ = split_lengths(
                self.load.total_len, self.prefix_share, align_blocks=align
            )
        except SweepError as error:
            # A pydantic validator reports only a ValueError as a validation error.
            raise empty_prefix from error
        # split_lengths lets a share that floors to 0 tokens through as an empty prefix.
        if prefix_len == 0:
            raise empty_prefix


def load_kv_ab_config(path: Path) -> KvAbConfig:
    """Read and validate the kv-cache-extension-ab config at ``path``.

    :param path: the config YAML, e.g. bench/kv-cache-extension-ab-agent.yaml.
    :return: the validated config.
    :raise KvAbConfigError: when the file is missing, unreadable, empty, not valid YAML,
        or fails validation.
    """
    return load_yaml_model(
        path, KvAbConfig, label="kv-ab config", error=KvAbConfigError
    )
