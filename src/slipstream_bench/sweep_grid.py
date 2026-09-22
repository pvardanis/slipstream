"""Load, validate, and emit the knob-sweep grid the `just knob-sweep` recipe reads.

bench/sweep-grid.yaml holds every value the sweep varies — nothing is hard-coded in
the recipe. This module parses it with PyYAML and validates it against the SweepGrid
pydantic model, so a malformed or out-of-range value fails here, before any GPU
redeploy, rather than mid-sweep on live hardware (ADR-0009). The `sweep-grid` CLI
emits three things the recipe loop reads: the Tier-1 points (one per row, keyed by
the slug an EnginePoint names), the Tier-2 --max-concurrency ladder, and the pinned
burstiness. Points reuse EnginePoint from sweep_aggregation so the grid emits, the
recipe writes, and the aggregator parses one slug format from one place.
"""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from slipstream_bench.sweep_aggregation import EnginePoint

# The KV-cache dtype and prefix-caching arms are keyed by their chart labels, the
# same tokens EnginePoint.from_dirname parses off a slug — constraining them here
# keeps the grid from emitting a slug the aggregator would later reject.
KvLabel = Literal["fp8", "fp16"]
PrefixCachingLabel = Literal["on", "off"]


def _unique(values: list[int]) -> list[int]:
    """Reject a repeated swept value: two equal points collide on one results subdir."""
    if len(set(values)) != len(values):
        raise ValueError("swept values must be unique, none repeated")
    return values


PositiveInts = Annotated[
    list[Annotated[int, Field(gt=0)]], Field(min_length=1), AfterValidator(_unique)
]
PrefixShares = Annotated[
    list[Annotated[int, Field(ge=0, le=100)]],
    Field(min_length=1),
    AfterValidator(_unique),
]
NonEmptyStr = Annotated[str, Field(min_length=1)]


class SweepGridError(Exception):
    """A sweep grid that cannot be read or does not validate."""


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

    max_num_seqs: PositiveInts
    kv_cache_dtype: Annotated[dict[KvLabel, NonEmptyStr], Field(min_length=1)]
    prefix_caching: Annotated[
        dict[PrefixCachingLabel, PrefixCachingArm], Field(min_length=1)
    ]


class Tier2(BaseModel):
    """Client load knobs, swept per Tier-1 point with no redeploy (ADR-0009)."""

    model_config = ConfigDict(extra="forbid")

    max_concurrency: PositiveInts
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
    if data is None:
        raise SweepGridError(f"sweep grid is empty: {path}")
    try:
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
    passes it straight through.
    """
    rows: list[str] = []
    for max_num_seqs in grid.tier1.max_num_seqs:
        for kv_label, kv_engine in grid.tier1.kv_cache_dtype.items():
            for pc_label, arm in grid.tier1.prefix_caching.items():
                point = EnginePoint(
                    max_num_seqs=max_num_seqs,
                    kv_cache_dtype=kv_label,
                    prefix_caching=pc_label == "on",
                )
                shares = ",".join(str(share) for share in arm.prefix_share)
                rows.append(
                    "\t".join(
                        [point.slug(), str(max_num_seqs), kv_engine, arm.flag, shares]
                    )
                )
    return "\n".join(rows)


def render_ladder(grid: SweepGrid) -> str:
    """Emit the Tier-2 --max-concurrency rungs, one per line."""
    return "\n".join(str(rung) for rung in grid.tier2.max_concurrency)


def render_burstiness(grid: SweepGrid) -> str:
    """Emit the pinned burstiness scalar the whole sweep runs at."""
    return str(grid.tier2.burstiness)
