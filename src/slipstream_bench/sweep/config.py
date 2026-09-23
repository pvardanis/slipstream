"""The knob set one load-sweep runs with, loaded from its YAML experiment definition.

``SweepConfig`` is the single validation layer: the experiment-defining knobs come
from a per-command YAML file (``bench/load-sweep.yaml``), the execution context
(endpoint, served model, output dir, commercial flag) is injected from the CLI, and
every field is range-checked as the model validates. ``load_sweep_config`` is the
boundary that reads the file, guards the CLI-injected keys the config must never set,
and rejects a malformed config before the sweep touches the endpoint.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from slipstream_bench.sweep.fields import (
    ConcurrencyLadder,
    GoodputSlo,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    PrefixShares,
    RequestRate,
    UniquePositiveFloats,
)


class SweepError(Exception):
    """A sweep input that cannot produce a meaningful measurement."""


# Injected from the CLI, never authored in the config YAML: the endpoint, the served
# model (model.yaml is its single source of truth, read via image-tag.sh hf-id), the
# output dir, and whether this is the commercial arm. The loader rejects a config that
# sets any of these, so the model SoT is never duplicated into the experiment file.
_RESERVED_KEYS = ("base_url", "model", "out_dir", "commercial")


class SweepConfig(BaseModel):
    """The knobs one sweep is run with.

    The grid is prefix_shares x burstiness_values x max_concurrency_values. An empty
    ``max_concurrency_values`` sweeps open-loop (arrival-rate bound by request_rate,
    no in-flight cap); a non-empty ladder sweeps closed-loop, capping in-flight
    requests with ``vllm bench serve --max-concurrency`` — the axis the concurrency
    ceiling is read off (ADR-0009).
    """

    # ``model`` is a served-model id, not a pydantic ``model_``-namespaced field, so
    # the protected namespace is cleared to name it plainly without a warning.
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    base_url: NonEmptyStr
    model: NonEmptyStr
    prefix_shares: PrefixShares
    burstiness_values: UniquePositiveFloats
    total_len: PositiveInt
    num_prompts: PositiveInt
    num_prefixes: PositiveInt
    output_len: PositiveInt
    align_blocks: NonNegativeInt
    request_rate: RequestRate
    seed: NonNegativeInt
    out_dir: NonEmptyStr
    goodput: GoodputSlo
    tokenizer: str | None = None
    commercial: bool = False
    max_concurrency_values: ConcurrencyLadder = []

    @model_validator(mode="after")
    def _reject_fewer_prompts_than_prefixes(self) -> "SweepConfig":
        """Reject a grid whose prompts cannot cover its prefixes.

        vLLM's prefix_repetition workload gives each prefix at least one prompt, so
        it rejects a run with more prefixes than prompts. Fail fast here rather than
        let every cell die the same way mid-grid.
        """
        if self.num_prompts < self.num_prefixes:
            raise ValueError(
                f"num_prompts {self.num_prompts} is below num_prefixes "
                f"{self.num_prefixes}: raise prompts or lower prefixes"
            )
        return self

    @model_validator(mode="after")
    def _require_tokenizer_when_commercial(self) -> "SweepConfig":
        """Reject a commercial sweep that has no local tokenizer for synthesis.

        A commercial ``model`` is a provider id (e.g. gpt-4o-mini) vLLM cannot load
        as an HF tokenizer, so prompt synthesis needs an explicit local one. The
        provider bills on its own tokenizer regardless; this only fixes the workload
        text, and every cell would die the same way without it.
        """
        if self.commercial and not (self.tokenizer and self.tokenizer.strip()):
            raise ValueError(
                "a commercial sweep (--api-key-env) needs a tokenizer: the provider "
                "model will not resolve as a local tokenizer for prompt synthesis"
            )
        return self


def load_sweep_config(
    path: Path,
    *,
    base_url: str,
    model: str,
    out_dir: str,
    commercial: bool,
) -> SweepConfig:
    """Read the experiment definition at ``path`` and bind the execution context.

    The YAML carries only what defines the experiment (the grid axes, lengths, SLO,
    seed); the endpoint, served model, output dir, and commercial flag are injected
    from the CLI, since the served model's single source of truth is model.yaml. A
    config that sets any reserved key, is missing, unreadable, empty, not a YAML
    mapping, or fails validation is rejected here, before the sweep touches the
    endpoint.

    :param path: the config YAML file, e.g. bench/load-sweep.yaml.
    :param base_url: the endpoint the sweep targets.
    :param model: the served model id (from model.yaml via image-tag.sh hf-id).
    :param out_dir: the directory for the per-cell result JSON.
    :param commercial: whether this is the commercial arm (drives the tokenizer guard).
    :return: the validated config.
    :raise SweepError: on any read/parse/validate failure, or a reserved key set.
    """
    try:
        text = path.read_text()
    except FileNotFoundError as error:
        raise SweepError(f"sweep config not found: {path}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise SweepError(f"sweep config could not be read: {path}: {error}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SweepError(f"{path} is not valid YAML: {error}") from error
    # safe_load returns None for an empty or comment-only file without raising; name
    # that here so the sweep aborts on a clear message, not an opaque "input should be
    # a mapping" from validating None.
    if data is None:
        raise SweepError(f"sweep config is empty: {path}")
    if not isinstance(data, dict):
        raise SweepError(f"sweep config must be a mapping: {path}")
    reserved = [key for key in _RESERVED_KEYS if key in data]
    if reserved:
        raise SweepError(
            f"{path} sets CLI-injected key(s) {', '.join(reserved)}: these come from "
            "the command line (model.yaml is the served-model source of truth), not "
            "the config file"
        )
    try:
        return SweepConfig.model_validate(
            {
                **data,
                "base_url": base_url,
                "model": model,
                "out_dir": out_dir,
                "commercial": commercial,
            }
        )
    except ValidationError as error:
        raise SweepError(f"invalid sweep config ({path}):\n{error}") from error
