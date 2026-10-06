"""Resolve one engine point's vLLM args from the knob-sweep grid.

The grid model and its loaders are the contract kernel
(:mod:`slipstream_bench.contract.grid`); this resolves a single Tier-1 point's engine
args — the ``max-num-seqs``, ``--kv-cache-dtype`` token, and prefix-caching flag the
worker renders into the GPU manifest before a redeploy (ADR-0015).
"""

from dataclasses import dataclass

from slipstream_bench.contract.grid import KvLabel, SweepGrid, require_grid_arm
from slipstream_bench.contract.records import EnginePoint

# The label->token mapping vLLM's --kv-cache-dtype accepts: the grid carries only the
# chart label, so the token is resolved here, the single place an in-cluster run reads it.
_KV_ENGINE_TOKEN: dict[KvLabel, str] = {"fp8": "fp8", "fp16": "float16"}


@dataclass(frozen=True)
class EngineArgs:
    """The three swept vLLM args one Tier-1 engine point runs with.

    A single engine point's engine args: ``max-num-seqs``, the ``--kv-cache-dtype``
    token (the chart label mapped to what vLLM accepts, fp16 -> float16), and the
    prefix-caching flag (``--enable-prefix-caching`` / ``--no-enable-prefix-caching``).
    Binding them into one value keeps a caller from threading three loose strings it
    could transpose, and reuses the one label->token mapping the manifest render reads.
    """

    max_num_seqs: int
    kv_engine_token: str
    prefix_caching_flag: str


def get_engine_args(grid: SweepGrid, point: EnginePoint) -> EngineArgs:
    """Resolve one engine point's vLLM args from the grid, their single source.

    Reads the three engine args a point runs with — max-num-seqs from the point, the
    kv-cache token from the grid's label->token mapping, and the caching flag from the
    point's arm — so an in-cluster run renders the point's manifest knobs from one
    mapping (ADR-0009).

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
