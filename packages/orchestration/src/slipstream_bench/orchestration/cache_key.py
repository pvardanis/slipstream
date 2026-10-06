"""The resume address of a cell: ``digest:point-slug:cell-name``.

ADR-0012: Prefect skips a cell when a ``cache_key_fn`` returns a key it has already
seen — the key is ``digest:point-slug:cell-name``, so config identity (the deep
digest), engine coordinates (the point slug), and the client-load rung (the cell name)
together address exactly one measurement. The pure :func:`build_cache_key` holds the
one format; :func:`get_cell_cache_key` is the Prefect-shaped adapter that reads the three
parts off a task's call parameters, kept import-light so no Prefect type leaks in.
"""

from collections.abc import Mapping
from typing import Any

# The parameter names the sweep task is called with that name a cell's resume
# address; the adapter reads exactly these three off the call.
_KEY_PARTS = ("digest", "point_slug", "cell_name")


class CacheKeyError(Exception):
    """A cache key that cannot address a cell — a blank component."""


def build_cache_key(*, digest: str, point_slug: str, cell_name: str) -> str:
    """Join the three parts into a cell's resume key, rejecting a blank component.

    :param digest: the deep config digest (config identity).
    :param point_slug: the engine-knob point slug, e.g. ``mns64_kvfp8_pcon``.
    :param cell_name: the client-load cell name, e.g. ``pshare50_mc64``.
    :return: ``digest:point-slug:cell-name``.
    :raise CacheKeyError: when any part is blank — a key with an empty component
        would collide cells that must resume independently.
    """
    _require("digest", digest)
    _require("point_slug", point_slug)
    _require("cell_name", cell_name)
    return f"{digest}:{point_slug}:{cell_name}"


def get_cell_cache_key(_context: object, parameters: Mapping[str, Any]) -> str:
    """Build a cell's cache key from a task run's call parameters (Prefect adapter).

    Prefect's ``cache_key_fn`` contract calls this positionally with the task run
    context and the call parameters. The context is unused — the key is a pure function
    of the parameters — so it is named ``_context`` to mark it intentionally unread.

    :param _context: the Prefect task run context, required by the contract, unused.
    :param parameters: the task's call parameters, carrying ``digest``,
        ``point_slug``, and ``cell_name``.
    :return: the cell's resume key.
    :raise CacheKeyError: when a key part is absent or blank.
    """
    missing = [part for part in _KEY_PARTS if part not in parameters]
    if missing:
        raise CacheKeyError(
            f"cache key parameters missing {', '.join(missing)}: cannot address a cell"
        )
    return build_cache_key(
        digest=parameters["digest"],
        point_slug=parameters["point_slug"],
        cell_name=parameters["cell_name"],
    )


def _require(name: str, value: str) -> None:
    """Reject a blank cache-key component."""
    if not isinstance(value, str) or not value.strip():
        raise CacheKeyError(f"cache key {name} is blank: cannot address a cell")
