"""The knob sets a sweep and a single cell run with, loaded from their YAML.

``LoadKnobs`` is the experiment-defining validation layer both a whole-grid sweep and a
single cell carry (token budget, SLO, seed); ``Knobs`` adds the execution context
(endpoint, served model, output dir, commercial flag) injected from the CLI, and every
field is range-checked as the model validates. ``SweepConfig`` adds the
grid axes and derives one ``CellConfig`` per point via :meth:`SweepConfig.cells`;
``CellConfig`` adds the single coordinate the container runs. ``load_sweep_config`` and
``load_cell_config`` are the boundaries that read the file, guard the CLI-injected keys
the config must never set, and reject a malformed config before the run touches the
endpoint.
"""

from collections.abc import Iterator
from itertools import product
from pathlib import Path
from typing import Self, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from slipstream_bench.sweep.fields import (
    Burstiness,
    ConcurrencyLadder,
    GoodputSlo,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    PrefixShare,
    PrefixShares,
    RequestRate,
    UniquePositiveFloats,
)


class SweepError(Exception):
    """A sweep input that cannot produce a meaningful measurement."""


# The config model a loader validates against: a whole-grid SweepConfig or a single
# CellConfig, both Knobs subclasses. Binds _load_config's return to the model passed in.
_KnobsT = TypeVar("_KnobsT", bound="Knobs")


class LoadKnobs(BaseModel):
    """The experiment-defining client-load knobs a sweep and a single cell share.

    The knobs that define the workload independent of where it runs: token budget,
    prompt and prefix counts, output length, prefix block alignment, arrival rate, SLO,
    seed, and the optional prompt-synthesis tokenizer. The knob grid (``SweepGrid``) and
    a standalone ``SweepConfig``/``CellConfig`` both carry these; ``Knobs`` layers the
    CLI-injected execution context on top. Separate from ``Knobs`` so ``SweepGrid`` can
    hold the fixed load knobs without the injected fields (endpoint, served model, out
    dir) it has no business setting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_len: PositiveInt
    num_prompts: PositiveInt
    num_prefixes: PositiveInt
    output_len: PositiveInt
    align_blocks: NonNegativeInt
    request_rate: RequestRate
    seed: NonNegativeInt
    goodput: GoodputSlo
    tokenizer: str | None = None

    @model_validator(mode="after")
    def _reject_fewer_prompts_than_prefixes(self) -> Self:
        """Reject knobs whose prompts cannot cover their prefixes.

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


class Knobs(LoadKnobs):
    """The load knobs plus the execution context injected from the CLI.

    ``LoadKnobs`` defines the experiment; this adds where it runs: the endpoint, the
    served model and its pinned revision (model.yaml is their single source of truth),
    the output dir, and the commercial-arm flag. A whole-grid ``SweepConfig`` and a
    single ``CellConfig`` extend this full model, so both range-check load knobs and
    context the same way; a config YAML never authors the injected fields (see
    ``RESERVED_KEYS``).

    ``revision`` pins the tokenizer's Hugging Face commit for ``vllm bench serve``: the
    image bakes the tokenizer under that commit sha with no ``refs/main``, so an offline
    client that asks for the default ``main`` ref cannot resolve it. ``None`` floats
    ``main`` for a model that pins no revision.
    """

    # ``model`` is a served-model id, not a pydantic ``model_``-namespaced field, so
    # the protected namespace is cleared to name it plainly without a warning.
    model_config = ConfigDict(protected_namespaces=())

    base_url: NonEmptyStr
    model: NonEmptyStr
    revision: str | None = None
    out_dir: NonEmptyStr
    commercial: bool = False

    @model_validator(mode="after")
    def _require_tokenizer_when_commercial(self) -> Self:
        """Reject a commercial run that has no local tokenizer for synthesis.

        A commercial ``model`` is a provider id (e.g. gpt-4o-mini) vLLM cannot load
        as an HF tokenizer, so prompt synthesis needs an explicit local one. The
        provider bills on its own tokenizer regardless; this only fixes the workload
        text, and every cell would die the same way without it.
        """
        if self.commercial and not (self.tokenizer and self.tokenizer.strip()):
            raise ValueError(
                "a commercial run (--api-key-env) needs a tokenizer: the provider "
                "model will not resolve as a local tokenizer for prompt synthesis"
            )
        return self


# Injected from the CLI, never authored in the config YAML: the fields ``Knobs`` adds
# on top of ``LoadKnobs`` — the endpoint, the served model (model.yaml is its single
# source of truth, read via image-tag.sh hf-id), the output dir, and the commercial
# flag. Derived from the type delta so it cannot drift from the split: the loader
# rejects a config that sets any of these, so the model SoT is never duplicated into
# the experiment file.
RESERVED_KEYS = tuple(
    name for name in Knobs.model_fields if name not in LoadKnobs.model_fields
)


class CellConfig(Knobs):
    """The knobs one cell is run with: the shared knobs plus its grid coordinate.

    The container executes one cell per ``docker run`` (ADR-0012 §Amendment), handed
    exactly this. ``max_concurrency`` set caps in-flight requests (closed-loop, the
    axis the concurrency ceiling is read off, ADR-0009); omitted runs the cell
    open-loop, arrival-rate bound by ``request_rate``.
    """

    prefix_share: PrefixShare
    burstiness: Burstiness
    max_concurrency: PositiveInt | None = None


class SweepConfig(Knobs):
    """The knobs and grid one whole sweep is run with.

    The grid is prefix_shares x burstiness_values x max_concurrency_values. An empty
    ``max_concurrency_values`` sweeps open-loop (arrival-rate bound by request_rate,
    no in-flight cap); a non-empty ladder sweeps closed-loop, capping in-flight
    requests with ``vllm bench serve --max-concurrency`` — the axis the concurrency
    ceiling is read off (ADR-0009).
    """

    prefix_shares: PrefixShares
    burstiness_values: UniquePositiveFloats
    max_concurrency_values: ConcurrencyLadder = []

    def cells(self) -> Iterator[CellConfig]:
        """Yield one ``CellConfig`` per grid point, shares outermost, ladder innermost.

        The max-concurrency ladder is the innermost axis, so every rung of it runs
        contiguously within one (share, burstiness) pair — the sequence the
        concurrency ceiling is read off. With no ladder configured the axis is a
        single open-loop ``None``, one cell per (share, burstiness) pair. Each cell
        carries the shared knobs verbatim; only the coordinate varies.
        """
        knobs = self.model_dump(
            exclude={"prefix_shares", "burstiness_values", "max_concurrency_values"}
        )
        ladder = self.max_concurrency_values or (None,)
        for share, burstiness, max_concurrency in product(
            self.prefix_shares, self.burstiness_values, ladder
        ):
            yield CellConfig(
                **knobs,
                prefix_share=share,
                burstiness=burstiness,
                max_concurrency=max_concurrency,
            )


def _read_config_mapping(path: Path, *, label: str) -> dict[str, object]:
    """Read a config YAML at ``path`` into a mapping, guarding the reserved keys.

    The shared boundary both loaders run: read the file, parse it, name an empty or
    non-mapping document, and reject a config that sets any CLI-injected key. The
    caller layers on the execution context and validates against its own model.

    :param path: the config YAML file.
    :param label: the config kind named in the error messages (e.g. "sweep config",
        "cell config"), so a failure names the artifact the caller actually passed.
    :return: the parsed mapping, with no reserved key set.
    :raise SweepError: on a read/parse failure, an empty or non-mapping document, or
        a reserved key set.
    """
    try:
        text = path.read_text()
    except FileNotFoundError as error:
        raise SweepError(f"{label} not found: {path}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise SweepError(f"{label} could not be read: {path}: {error}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SweepError(f"{path} is not valid YAML: {error}") from error
    # safe_load returns None for an empty or comment-only file without raising; name
    # that here so the run aborts on a clear message, not an opaque "input should be
    # a mapping" from validating None.
    if data is None:
        raise SweepError(f"{label} is empty: {path}")
    if not isinstance(data, dict):
        raise SweepError(f"{label} must be a mapping: {path}")
    reserved = [key for key in RESERVED_KEYS if key in data]
    if reserved:
        raise SweepError(
            f"{path} sets CLI-injected key(s) {', '.join(reserved)}: these come from "
            "the command line (model.yaml is the served-model source of truth), not "
            "the config file"
        )
    return data


def _load_config(
    path: Path,
    model_cls: type[_KnobsT],
    *,
    label: str,
    base_url: str,
    model: str,
    out_dir: str,
    commercial: bool,
    revision: str | None = None,
) -> _KnobsT:
    """Read a config YAML at ``path``, bind the execution context, and validate it.

    The shared body both loaders run: the endpoint, served model, output dir, and
    commercial flag are injected from the CLI, since the served model's single source
    of truth is model.yaml. A config that sets any reserved key, is missing,
    unreadable, empty, not a YAML mapping, or fails validation is rejected here,
    before the run touches the endpoint.

    :param path: the config YAML file.
    :param model_cls: the model the mapping validates against (SweepConfig/CellConfig).
    :param label: the config kind named in error messages (e.g. "sweep config").
    :param base_url: the endpoint the run targets.
    :param model: the served model id (from model.yaml via image-tag.sh hf-id).
    :param out_dir: the directory for the per-cell result JSON.
    :param commercial: whether this is the commercial arm (drives the tokenizer guard).
    :param revision: the model's pinned Hugging Face commit, injected so the cell
        command can pin the tokenizer to the baked snapshot; None floats ``main``.
    :return: the validated config.
    :raise SweepError: on any read/parse/validate failure, or a reserved key set.
    """
    data = _read_config_mapping(path, label=label)
    try:
        return model_cls.model_validate(
            {
                **data,
                "base_url": base_url,
                "model": model,
                "revision": revision,
                "out_dir": out_dir,
                "commercial": commercial,
            }
        )
    except ValidationError as error:
        raise SweepError(f"invalid {label} ({path}):\n{error}") from error


def load_sweep_config(
    path: Path,
    *,
    base_url: str,
    model: str,
    out_dir: str,
    commercial: bool,
    revision: str | None = None,
) -> SweepConfig:
    """Read the sweep definition at ``path`` (grid axes + shared knobs) and validate it.

    The YAML carries only what defines the experiment (the grid axes, lengths, SLO,
    seed); the execution context is injected from the CLI. See :func:`_load_config`.

    :param path: the config YAML file, e.g. bench/load-sweep.yaml.
    :param base_url: the endpoint the sweep targets.
    :param model: the served model id (from model.yaml via image-tag.sh hf-id).
    :param out_dir: the directory for the per-cell result JSON.
    :param commercial: whether this is the commercial arm (drives the tokenizer guard).
    :param revision: the model's pinned Hugging Face commit; None floats ``main``.
    :return: the validated config.
    :raise SweepError: on any read/parse/validate failure, or a reserved key set.
    """
    return _load_config(
        path,
        SweepConfig,
        label="sweep config",
        base_url=base_url,
        model=model,
        out_dir=out_dir,
        commercial=commercial,
        revision=revision,
    )


def load_cell_config(
    path: Path,
    *,
    base_url: str,
    model: str,
    out_dir: str,
    commercial: bool,
    revision: str | None = None,
) -> CellConfig:
    """Read the single-cell definition at ``path`` (coordinate + shared knobs).

    The YAML carries the shared knobs (lengths, SLO, seed) and the one coordinate the
    cell runs (prefix_share, burstiness, optional max_concurrency); the execution context is
    injected from the CLI. The coordinate is range-checked as the ``CellConfig`` fields
    validate. See :func:`_load_config`.

    :param path: the config YAML file, e.g. bench/load-cell.yaml.
    :param base_url: the endpoint the cell targets.
    :param model: the served model id (from model.yaml via image-tag.sh hf-id).
    :param out_dir: the directory for this cell's result JSON.
    :param commercial: whether this is the commercial arm (drives the tokenizer guard).
    :param revision: the model's pinned Hugging Face commit; None floats ``main``.
    :return: the validated cell config.
    :raise SweepError: on any read/parse/validate failure, or a reserved key set.
    """
    return _load_config(
        path,
        CellConfig,
        label="cell config",
        base_url=base_url,
        model=model,
        out_dir=out_dir,
        commercial=commercial,
        revision=revision,
    )
