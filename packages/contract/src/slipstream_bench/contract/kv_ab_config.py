"""The kv-cache-extension-ab workload config: what both arms of the A/B run.

bench/kv-cache-extension-ab.yaml holds the workload the KV cache offload A/B measures
under each arm: the engine knobs held fixed across both arms, the prefix share, the
concurrency ladder, the warmup rung, the runs per arm, and the client-load knobs the knob
sweep's cells share (``LoadKnobs``). It is validated on load, so a malformed workload fails
before the first GPU redeploy. The rig's capacities — the CPU tier and max-model-len — are
not here: the orchestration worker reads them off the vLLM manifest that configures them.
"""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from slipstream_bench.contract.config import LoadKnobs, SweepError
from slipstream_bench.contract.fields import PositiveInt, UniquePositiveInts
from slipstream_bench.contract.naming import split_lengths
from slipstream_bench.contract.records import KvLabel

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
    kv_cache_dtype: KvLabel


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
            block size, or the alignment floors the prefix to nothing.
        """
        align = self.load.align_blocks
        if align <= 0 or align % _VLLM_BLOCK_TOKENS:
            raise ValueError(
                f"load.align_blocks {align} must be a positive multiple of vLLM's "
                f"{_VLLM_BLOCK_TOKENS}-token KV block, so every prefix is fully cached"
            )
        try:
            split_lengths(self.load.total_len, self.prefix_share, align_blocks=align)
        except SweepError as error:
            # A pydantic validator reports only a ValueError as a validation error.
            raise ValueError(str(error)) from error
        return self

    @property
    def prefix_len(self) -> int:
        """The tokens of each shared prefix, floored to whole KV blocks."""
        prefix_len, _ = split_lengths(
            self.load.total_len, self.prefix_share, align_blocks=self.load.align_blocks
        )
        return prefix_len

    @property
    def working_set_tokens(self) -> int:
        """The reusable tokens the workload cycles through: every distinct prefix."""
        return self.load.num_prefixes * self.prefix_len


def load_kv_ab_config(path: Path) -> KvAbConfig:
    """Read and validate the kv-cache-extension-ab config at ``path``.

    :param path: the config YAML, e.g. bench/kv-cache-extension-ab.yaml.
    :return: the validated config.
    :raise KvAbConfigError: when the file is missing, unreadable, empty, not valid YAML,
        or fails validation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise KvAbConfigError(f"kv-ab config not found: {path}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise KvAbConfigError(
            f"kv-ab config could not be read: {path}: {error}"
        ) from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise KvAbConfigError(f"{path} is not valid YAML: {error}") from error
    if data is None:
        raise KvAbConfigError(f"kv-ab config is empty: {path}")
    try:
        return KvAbConfig.model_validate(data)
    except ValidationError as error:
        raise KvAbConfigError(f"invalid kv-ab config ({path}):\n{error}") from error
