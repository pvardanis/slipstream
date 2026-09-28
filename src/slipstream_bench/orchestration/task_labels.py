"""Prefect run tags for a bench cell: one filterable key=value per tier knob.

Turns a cell's grid coordinate — the tier1 engine point (parsed off its slug) and the
tier2 client-load :class:`CellConfig` — into the tags Prefect shows per task run, so the
UI filters a run by any single knob (every ``kv=fp8`` run, the whole ``mns=64`` deploy's
concurrency ladder) rather than only the composite ``point:cell`` name.
"""

from slipstream_bench.sweep.aggregation import EnginePoint
from slipstream_bench.sweep.config import CellConfig


def get_cell_run_tags(point_slug: str, cell: CellConfig) -> list[str]:
    """Tag one cell run with each tier1 and tier2 knob as a filterable ``key=value``.

    tier1 is parsed off the engine point slug; tier2 is read from the cell. An
    open-loop cell (no concurrency cap) tags ``mc=open`` so it still filters cleanly.

    :param point_slug: the engine point slug (``mns{N}_kv{fp8|fp16}_pc{on|off}``).
    :param cell: the client-load cell the tier2 tags describe.
    :return: the knob tags, tier1 then tier2.
    """
    point = EnginePoint.from_dirname(point_slug)
    cap = "open" if cell.max_concurrency is None else str(cell.max_concurrency)
    return [
        f"mns={point.max_num_seqs}",
        f"kv={point.kv_cache_dtype}",
        f"pc={'on' if point.prefix_caching else 'off'}",
        f"pshare={cell.prefix_share}",
        f"burst={cell.burstiness}",
        f"mc={cap}",
    ]
