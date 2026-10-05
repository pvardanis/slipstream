"""The knob sets a sweep and a single cell run with: the pure config models.

``LoadKnobs`` is the experiment-defining validation layer both a whole-grid sweep and a
single cell carry (token budget, SLO, seed); ``Knobs`` adds the execution context
(endpoint, served model, output dir, commercial flag) injected from the CLI, and every
field is range-checked as the model validates. ``SweepConfig`` adds the grid axes and
derives one ``CellConfig`` per point via :meth:`SweepConfig.cells`; ``CellConfig`` adds
the single coordinate the container runs. The loaders that read these from YAML and bind
the CLI-injected context live in the executor (``slipstream_bench.sweep.config``); this
kernel holds the models and ``RESERVED_KEYS`` both the executor and the worker agree on.
"""

from collections.abc import Iterator
from itertools import product
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator
from slipstream.contract.fields import (
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
    served model (model.yaml is its single source of truth), the output dir, and the
    commercial-arm flag. A whole-grid ``SweepConfig`` and a
    single ``CellConfig`` extend this full model, so both range-check load knobs and
    context the same way; a config YAML never authors the injected fields (see
    ``RESERVED_KEYS``).
    """

    # ``model`` is a served-model id, not a pydantic ``model_``-namespaced field, so
    # the protected namespace is cleared to name it plainly without a warning.
    model_config = ConfigDict(protected_namespaces=())

    base_url: NonEmptyStr
    model: NonEmptyStr
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
