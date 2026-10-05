"""Render the knob-sweep grid slices the `just knob-sweep` recipe reads.

The grid model and its loaders are the contract kernel (:mod:`slipstream.contract.grid`);
this module renders the slices the recipe loop reads: the Tier-1 points (one per row, keyed
by the slug an EnginePoint names), the Tier-2 --max-concurrency ladder, and the pinned
burstiness.
"""

from dataclasses import dataclass
from enum import StrEnum
from itertools import product

from slipstream.contract.grid import KvLabel, SweepGrid, require_grid_arm
from slipstream.contract.records import EnginePoint

# The label->token mapping vLLM's --kv-cache-dtype accepts: the grid carries only the
# chart label, so the token is resolved here, the single place both the recipe's TSV and
# an in-cluster run read it from.
_KV_ENGINE_TOKEN: dict[KvLabel, str] = {"fp8": "fp8", "fp16": "float16"}


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
    arm = require_grid_arm(grid, point, point.slug())
    # require_grid_arm has confirmed the point's dtype is one the grid sweeps, so this
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
