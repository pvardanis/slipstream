"""Enumerate an engine point's cell result objects in S3, keyed as the executor wrote them.

A point's cells key the same whatever endpoint or output dir they ran against (ADR-0012):
the grid is their single source, so enumerating a point's objects needs only the grid, the
knob sweep's run prefix, and the served model, with non-empty placeholders for the endpoint
and output dir the cell addressing ignores. The redeploy-skip gate
(:mod:`slipstream_bench.orchestration.completion`) and the terminal result render
(:mod:`slipstream_bench.orchestration.tasks.render`) both read a point's objects this way, so the
enumeration lives here once rather than once per reader.
"""

from collections.abc import Iterator

from slipstream_bench.contract import (
    EnginePoint,
    SweepGrid,
    build_point_sweep_config,
    get_cell_basename,
)
from slipstream_bench.orchestration.cell_run import get_cell_s3_key

# The point's cells key the same way whatever endpoint or output dir they ran against, so
# enumerating them needs only non-empty placeholders for the two the addressing ignores.
_PLACEHOLDER_BASE_URL = "http://127.0.0.1:0"
_PLACEHOLDER_OUT_DIR = "/tmp"


def iter_point_cell_objects(
    point: EnginePoint,
    *,
    grid: SweepGrid,
    run_prefix: str,
    model: str,
    commercial: bool = False,
) -> Iterator[tuple[str, str]]:
    """Yield ``(s3_key, basename)`` for each of an engine point's cells.

    Enumerates the point's cells from the grid — their single source — and addresses each
    under ``sweeps/<run_prefix>/<point-slug>/``, the prefix the executor wrote it to. The
    basename is the object's own name, so a caller downloads the key to a local path keyed
    the same (``<point-slug>/<basename>``), the layout the aggregators read.

    :param point: the engine-knob point whose cells are enumerated.
    :param grid: the validated grid the point's cells are built from.
    :param run_prefix: the knob sweep's shared run id (the cells nest under
        ``<run_prefix>/<point-slug>``).
    :param model: the served model id (folded into the point's sweep config).
    :param commercial: whether this is the commercial arm (matches the run's config).
    :return: the ``(s3_key, basename)`` pair for each cell, in the grid's cell order.
    :raise SweepGridError: when the point's knobs are not an arm this grid sweeps (see
        :func:`slipstream_bench.contract.build_point_sweep_config`).
    """
    config = build_point_sweep_config(
        grid,
        point.slug(),
        base_url=_PLACEHOLDER_BASE_URL,
        model=model,
        out_dir=_PLACEHOLDER_OUT_DIR,
        commercial=commercial,
    )
    run_id = f"{run_prefix}/{point.slug()}"
    for cell in config.cells():
        yield get_cell_s3_key(run_id, cell), get_cell_basename(cell)
