"""Load, validate, and emit the knob-sweep grid the `just knob-sweep` recipe reads.

bench/sweep-grid.yaml holds every value the sweep varies — nothing is hard-coded in
the recipe. This module parses it with PyYAML and validates it against the SweepGrid
pydantic model, so a malformed or out-of-range value fails here, before any GPU
redeploy, rather than mid-sweep on live hardware (ADR-0009). The `sweep-grid` CLI
emits three things the recipe loop reads: the Tier-1 points (one per row, keyed by
the slug an EnginePoint names), the Tier-2 --max-concurrency ladder, and the pinned
burstiness. Points reuse EnginePoint from sweep.aggregation so the grid emits, the
recipe writes, and the aggregator parses one slug format from one place.

The grid also carries the fixed client-load knobs (a ``load`` section) every cell runs
with, so :func:`build_point_sweep_config` can fold a single engine point's caching-arm
shares, pinned burstiness, and concurrency ladder into the ``SweepConfig`` the
orchestration driver enumerates — the grid alone, not a second load-sweep file, being
the single source of a point's cells (ADR-0012).
"""

from dataclasses import dataclass
from enum import StrEnum
from itertools import product
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from slipstream_bench.sweep.aggregation import EnginePoint, SweepAggregationError
from slipstream_bench.sweep.config import LoadKnobs, SweepConfig
from slipstream_bench.sweep.fields import (
    NonEmptyStr,
    PrefixShares,
    UniquePositiveInts,
    unique,
)

# The KV-cache dtype and prefix-caching arms are keyed by their chart labels, the
# same tokens EnginePoint.from_dirname parses off a slug. The grid carries only the
# label; the token vLLM's --kv-cache-dtype accepts is mapped here, so a rename of a
# vLLM token is a one-line edit in code, not a change every grid file must copy.
KvLabel = Literal["fp8", "fp16"]
PrefixCachingLabel = Literal["on", "off"]

_KV_ENGINE_TOKEN: dict[KvLabel, str] = {"fp8": "fp8", "fp16": "float16"}

KvLabels = Annotated[list[KvLabel], Field(min_length=1), AfterValidator(unique)]


class SweepGridError(Exception):
    """A sweep grid that cannot be read or does not validate."""


class SweepGridPart(StrEnum):
    """A slice of the grid the knob-sweep loop asks the `sweep-grid` CLI for.

    - ``engine_points``: the Tier-1 engine points as TSV, one manifest redeploy per
      row, each keyed by its results-subdir slug (mns{N}_kv{fp8|fp16}_pc{on|off}).
    - ``concurrency_ladder``: the Tier-2 --max-concurrency rungs, one per line — the
      ceiling search the recipe raises until goodput drops below the SLO.
    - ``burstiness``: the single pinned scalar the whole sweep runs at.
    """

    engine_points = "engine-points"
    concurrency_ladder = "concurrency-ladder"
    burstiness = "burstiness"


class PrefixCachingArm(BaseModel):
    """One prefix-caching condition: its deploy flag and the shares swept under it.

    Caching-off reuses no prefix KV, so its arm carries a single 0 baseline while
    caching-on carries the {10, 50, 90} share sweep (ADR-0009) — the shares travel
    with the arm they are meaningful under, never as a null cell.
    """

    model_config = ConfigDict(extra="forbid")

    flag: NonEmptyStr
    prefix_share: PrefixShares


class Tier1(BaseModel):
    """Engine-arg knobs, one manifest redeploy per point (ADR-0009)."""

    model_config = ConfigDict(extra="forbid")

    max_num_seqs: UniquePositiveInts
    kv_cache_dtype: KvLabels
    prefix_caching: Annotated[
        dict[PrefixCachingLabel, PrefixCachingArm], Field(min_length=1)
    ]

    @model_validator(mode="after")
    def _off_arm_pins_the_zero_share(self) -> "Tier1":
        """Hold the caching-off arm to a single 0 share.

        With caching off vLLM reuses no prefix KV, so sweeping share there measures
        a definitional null (ADR-0009). Any share but the single [0] baseline is a
        grid mistake, caught here before the sweep runs.
        """
        off = self.prefix_caching.get("off")
        if off is not None and off.prefix_share != [0]:
            raise ValueError(
                "prefix_caching 'off' reuses no prefix KV; its prefix_share must be "
                f"the single [0] baseline, not {off.prefix_share}"
            )
        return self


class Tier2(BaseModel):
    """Client load knobs, swept per Tier-1 point with no redeploy (ADR-0009)."""

    model_config = ConfigDict(extra="forbid")

    max_concurrency: UniquePositiveInts
    burstiness: Annotated[float, Field(gt=0)]


class SweepGrid(BaseModel):
    """The whole knob-sweep grid: the Tier-1 engine points and Tier-2 client ladder.

    ``load`` holds the fixed client-load knobs (token budget, SLO, seed) every cell
    runs with, so the grid alone — not a second load-sweep file — is the single source
    of a point's cells the orchestration driver enumerates (ADR-0012).
    """

    model_config = ConfigDict(extra="forbid")

    tier1: Tier1
    tier2: Tier2
    load: LoadKnobs


def load_grid(path: Path) -> SweepGrid:
    """Read and validate the sweep grid at ``path``.

    :param path: the grid YAML file, e.g. bench/sweep-grid.yaml.
    :return: the validated grid.
    :raise SweepGridError: when the file is missing, unreadable, empty, not valid
        YAML, or fails validation — so the whole grid is checked before the sweep
        touches a GPU.
    """
    try:
        text = path.read_text()
    except FileNotFoundError as error:
        raise SweepGridError(f"sweep grid not found: {path}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise SweepGridError(
            f"sweep grid could not be read: {path}: {error}"
        ) from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SweepGridError(f"{path} is not valid YAML: {error}") from error
    # safe_load returns None for an empty or comment-only file without raising;
    # name that here so the sweep aborts on a clear message, not an opaque
    # "input should be a mapping" from validating None.
    if data is None:
        raise SweepGridError(f"sweep grid is empty: {path}")
    try:
        # model_validate, not SweepGrid(**data): a list or scalar top level would
        # make **data raise TypeError past this handler; model_validate turns any
        # non-mapping into the ValidationError we wrap.
        return SweepGrid.model_validate(data)
    except ValidationError as error:
        raise SweepGridError(f"invalid sweep grid ({path}):\n{error}") from error


def build_point_sweep_config(
    grid: SweepGrid,
    point_slug: str,
    *,
    base_url: str,
    model: str,
    out_dir: str,
    commercial: bool,
) -> SweepConfig:
    """Build one engine point's Tier-2 sweep from the grid, the point's single source.

    The grid holds every axis a point's cells span — the prefix shares under the
    point's caching arm, the pinned burstiness, and the ``--max-concurrency`` ladder —
    and the fixed load knobs in ``grid.load``. This folds them, for the point the slug
    names, into the ``SweepConfig`` whose :meth:`SweepConfig.cells` the driver
    enumerates, so no second load-sweep file can disagree with the grid the digest is
    taken over (ADR-0012).

    :param grid: the validated knob-sweep grid.
    :param point_slug: the engine point's slug (``mns{N}_kv{fp8|fp16}_pc{on|off}``),
        naming the Tier-1 knobs the point was redeployed with.
    :param base_url: the endpoint the cells target (injected, not in the grid).
    :param model: the served model id (from model.yaml, the model source of truth).
    :param out_dir: the directory for each cell's result JSON.
    :param commercial: whether this is the commercial arm (drives the tokenizer guard).
    :return: the point's ``SweepConfig``: its arm's shares x the pinned burstiness x
        the concurrency ladder, carrying the grid's load knobs and the injected context.
    :raise SweepGridError: when the slug is not a point subdir name, names Tier-1
        knobs this grid does not sweep (a mismatch would otherwise run the wrong
        cells), or the grid's load knobs cannot build a valid sweep for the point —
        e.g. a commercial arm whose ``grid.load`` pins no tokenizer.
    """
    point = _parse_point_slug(point_slug)
    arm = _require_grid_arm(grid, point, point_slug)
    try:
        return SweepConfig(
            **grid.load.model_dump(),
            base_url=base_url,
            model=model,
            out_dir=out_dir,
            commercial=commercial,
            prefix_shares=arm.prefix_share,
            burstiness_values=[grid.tier2.burstiness],
            max_concurrency_values=grid.tier2.max_concurrency,
        )
    except ValidationError as error:
        raise SweepGridError(
            f"grid load knobs cannot build a valid sweep for {point_slug!r}:\n{error}"
        ) from error


def list_engine_points(grid: SweepGrid) -> list[EnginePoint]:
    """Enumerate the grid's Tier-1 engine points, one per GPU redeploy.

    The cartesian product of the three engine knobs — max-num-seqs x kv-cache-dtype x
    prefix-caching arm — as :class:`EnginePoint` objects, the point-object form of the
    same enumeration :func:`render_points` emits as TSV. The parent knob-sweep flow
    iterates these to drive one point sweep per point (ADR-0015), reading the grid as
    the single source of the points so the flow and the recipe never enumerate apart.
    """
    return [
        EnginePoint(
            max_num_seqs=max_num_seqs,
            kv_cache_dtype=kv_label,
            prefix_caching=pc_label == "on",
        )
        for max_num_seqs, kv_label, pc_label in product(
            grid.tier1.max_num_seqs,
            grid.tier1.kv_cache_dtype,
            grid.tier1.prefix_caching,
        )
    ]


@dataclass(frozen=True)
class EngineArgs:
    """The three swept vLLM args one Tier-1 engine point runs with.

    A single engine point's engine args: ``max-num-seqs``, the ``--kv-cache-dtype``
    token (the chart label mapped to what vLLM accepts, fp16 -> float16), and the
    prefix-caching flag (``--enable-prefix-caching`` / ``--no-enable-prefix-caching``).
    Binding them into one value keeps a caller from threading three loose strings it
    could transpose, and reuses the one label->token mapping ``render_points`` emits.
    """

    max_num_seqs: int
    kv_engine_token: str
    prefix_caching_flag: str


def get_engine_args(grid: SweepGrid, point: EnginePoint) -> EngineArgs:
    """Resolve one engine point's vLLM args from the grid, their single source.

    Reads the three engine args a point runs with — max-num-seqs from the point, the
    kv-cache token from the grid's label->token mapping, and the caching flag from the
    point's arm — so every consumer of a point's args (the recipe's TSV, an in-cluster
    run) renders the same values from one mapping (ADR-0009).

    :param grid: the validated knob-sweep grid.
    :param point: the engine-knob point whose args are resolved.
    :return: the point's engine args (max-num-seqs, kv-cache token, caching flag).
    :raise SweepGridError: when the point's knobs are not all swept by this grid — the
        same fail-fast guard ``build_point_sweep_config`` applies, so a point the grid
        does not sweep never resolves args.
    """
    arm = _require_grid_arm(grid, point, point.slug())
    # _require_grid_arm has confirmed the point's dtype is one the grid sweeps, so this
    # finds the KvLabel-typed key the token mapping is keyed by (no unchecked cast).
    kv_label = next(
        label for label in grid.tier1.kv_cache_dtype if label == point.kv_cache_dtype
    )
    return EngineArgs(
        max_num_seqs=point.max_num_seqs,
        kv_engine_token=_KV_ENGINE_TOKEN[kv_label],
        prefix_caching_flag=arm.flag,
    )


def render_points(grid: SweepGrid) -> str:
    """Emit the Tier-1 points as TSV, one row per engine-knob redeploy.

    Each row is ``slug<TAB>max_num_seqs<TAB>kv_engine_token<TAB>prefix_caching_flag
    <TAB>prefix_share_csv``: the recipe reads the slug as the point's results subdir,
    the max-num-seqs / engine token / flag as the deploy's env, and the CSV as the
    per-point prefix-share sweep (Tier-2, no redeploy). The chart label (fp8/fp16)
    becomes the engine token vLLM accepts (fp16 -> float16) here, so the recipe
    passes it straight through. Emitting text, not objects, is the CLI seam: the
    consumer is a bash `read` loop that parses these tab-separated fields.
    """
    return "\n".join(
        "\t".join(
            [
                EnginePoint(
                    max_num_seqs=max_num_seqs,
                    kv_cache_dtype=kv_label,
                    prefix_caching=pc_label == "on",
                ).slug(),
                str(max_num_seqs),
                _KV_ENGINE_TOKEN[kv_label],
                arm.flag,
                ",".join(str(share) for share in arm.prefix_share),
            ]
        )
        for max_num_seqs, kv_label, (pc_label, arm) in product(
            grid.tier1.max_num_seqs,
            grid.tier1.kv_cache_dtype,
            grid.tier1.prefix_caching.items(),
        )
    )


def render_ladder(grid: SweepGrid) -> str:
    """Emit the Tier-2 --max-concurrency rungs, one per line."""
    return "\n".join(str(rung) for rung in grid.tier2.max_concurrency)


def render_burstiness(grid: SweepGrid) -> str:
    """Emit the pinned burstiness scalar the whole sweep runs at."""
    return str(grid.tier2.burstiness)


_RENDERERS = {
    SweepGridPart.engine_points: render_points,
    SweepGridPart.concurrency_ladder: render_ladder,
    SweepGridPart.burstiness: render_burstiness,
}


def render_part(part: SweepGridPart, grid: SweepGrid) -> str:
    """Emit the grid slice ``part`` names, as the knob-sweep loop reads it."""
    return _RENDERERS[part](grid)


def _parse_point_slug(point_slug: str) -> EnginePoint:
    """Parse an engine point off its slug, as a grid-level fail-fast error.

    :param point_slug: the point subdir slug (``mns{N}_kv{fp8|fp16}_pc{on|off}``).
    :return: the engine-knob point the slug names.
    :raise SweepGridError: when the slug is not a valid point subdir name.
    """
    try:
        return EnginePoint.from_dirname(point_slug)
    except SweepAggregationError as error:
        raise SweepGridError(str(error)) from error


def _require_grid_arm(
    grid: SweepGrid, point: EnginePoint, point_slug: str
) -> PrefixCachingArm:
    """Return the prefix-caching arm for a point the grid actually sweeps.

    The slug's Tier-1 knobs must all be in the grid: a slug naming a max-num-seqs,
    kv-dtype, or caching arm the grid does not sweep would silently run the wrong
    cells, so it is rejected here.

    :param grid: the validated knob-sweep grid.
    :param point: the engine-knob point parsed from the slug.
    :param point_slug: the original slug, for the error message.
    :return: the point's prefix-caching arm (its flag and swept shares).
    :raise SweepGridError: when the point's knobs are not all swept by the grid.
    """
    arm_label: PrefixCachingLabel = "on" if point.prefix_caching else "off"
    arm = grid.tier1.prefix_caching.get(arm_label)
    if (
        point.max_num_seqs not in grid.tier1.max_num_seqs
        or point.kv_cache_dtype not in grid.tier1.kv_cache_dtype
        or arm is None
    ):
        raise SweepGridError(
            f"{point_slug!r} is not a point this grid sweeps "
            f"(max_num_seqs {grid.tier1.max_num_seqs}, kv_cache_dtype "
            f"{grid.tier1.kv_cache_dtype}, prefix_caching "
            f"{sorted(grid.tier1.prefix_caching)})"
        )
    return arm
