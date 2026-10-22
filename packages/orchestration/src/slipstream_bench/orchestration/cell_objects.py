"""Address and enumerate a knob sweep's cell result objects in S3, keyed as the host wrote them.

Every knob-sweep object nests under the bucket's ``knob-sweep/`` prefix
(:func:`get_knob_sweep_prefix`), apart from any other experiment's.

A point's cells key the same whatever endpoint or output dir they ran against (ADR-0012):
the grid is their single source, so enumerating a point's objects needs only the grid, the
knob sweep's run prefix, and the served model, with non-empty placeholders for the endpoint
and output dir the cell addressing ignores. The terminal result render
(:mod:`slipstream_bench.orchestration.tasks.render`) reads a point's objects this way, and the
redeploy-skip gate (:mod:`slipstream_bench.orchestration.flows.knob_sweep`) enumerates a
point's cell names from the same basenames, so the enumeration lives here once rather than
once per reader.
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

# The prefix every knob sweep's cells and charts nest under.
_KNOB_SWEEP_PREFIX = "knob-sweep"


def get_knob_sweep_prefix(run_id: str) -> str:
    """Root a knob-sweep run id under the bucket's knob-sweep prefix.

    :param run_id: the knob sweep's run id, ``<run>`` or a point's ``<run>/<point-slug>``.
    :return: the bucket prefix, ``knob-sweep/<run_id>``, without a trailing slash.
    """
    return f"{_KNOB_SWEEP_PREFIX}/{run_id}"


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
    under ``<run_prefix>/<point-slug>/``, the prefix the executor wrote it to. The
    basename is the object's own name, so a caller downloads the key to a local path keyed
    the same (``<point-slug>/<basename>``), the layout the aggregators read.

    :param point: the engine-knob point whose cells are enumerated.
    :param grid: the validated grid the point's cells are built from.
    :param run_prefix: the knob sweep's rooted run prefix, ``knob-sweep/<run>`` (the
        cells nest under ``<run_prefix>/<point-slug>``).
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
