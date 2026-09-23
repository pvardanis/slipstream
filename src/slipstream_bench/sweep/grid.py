"""Load, validate, and emit the knob-sweep grid the `just knob-sweep` recipe reads.

bench/sweep-grid.yaml holds every value the sweep varies — nothing is hard-coded in
the recipe. This module parses it with PyYAML and validates it against the SweepGrid
pydantic model, so a malformed or out-of-range value fails here, before any GPU
redeploy, rather than mid-sweep on live hardware (ADR-0009). The `sweep-grid` CLI
emits three things the recipe loop reads: the Tier-1 points (one per row, keyed by
the slug an EnginePoint names), the Tier-2 --max-concurrency ladder, and the pinned
burstiness. Points reuse EnginePoint from sweep.aggregation so the grid emits, the
recipe writes, and the aggregator parses one slug format from one place.
"""

from enum import Enum
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

from slipstream_bench.sweep.aggregation import EnginePoint
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


class SweepGridPart(str, Enum):
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
    """The whole knob-sweep grid: the Tier-1 engine points and Tier-2 client ladder."""

    model_config = ConfigDict(extra="forbid")

    tier1: Tier1
    tier2: Tier2


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
