"""The cache key is ``digest:point-slug:cell-name`` — the resume address of a cell.

ADR-0012: a Prefect ``cache_key_fn`` returns ``digest:point-slug:cell-name``; on a hit
the task enters ``Cached`` and does not run. The digest carries config identity, the
point slug the engine coordinates, and the cell name the client-load rung, so the
three together address exactly one measurement. The pure builder is tested without
Prefect; ``get_cell_cache_key`` reads the same three off a task's call parameters.
"""

import pytest

from slipstream_bench.orchestration.cache_key import (
    CacheKeyError,
    build_cache_key,
    get_cell_cache_key,
)


def test_build_cache_key_joins_the_three_parts() -> None:
    key = build_cache_key(
        digest="abc123", point_slug="mns64_kvfp8_pcon", cell_name="pshare50_mc64"
    )

    assert key == "abc123:mns64_kvfp8_pcon:pshare50_mc64"


def test_get_cell_cache_key_reads_the_parts_off_the_call_parameters() -> None:
    parameters = {
        "digest": "abc123",
        "point_slug": "mns64_kvfp8_pcon",
        "cell_name": "pshare50_mc64",
        "result_uri": "s3://bucket/sweeps/run/cell.json",
    }

    assert (
        get_cell_cache_key(None, parameters) == "abc123:mns64_kvfp8_pcon:pshare50_mc64"
    )


@pytest.mark.parametrize("missing", ["digest", "point_slug", "cell_name"])
def test_a_blank_part_is_rejected(missing: str) -> None:
    parts = {"digest": "abc123", "point_slug": "mns64", "cell_name": "pshare50"}
    parts[missing] = "  "

    with pytest.raises(CacheKeyError, match=missing):
        build_cache_key(**parts)


@pytest.mark.parametrize("absent", ["digest", "point_slug", "cell_name"])
def test_get_cell_cache_key_rejects_an_absent_call_parameter(absent: str) -> None:
    parameters = {"digest": "abc123", "point_slug": "mns64", "cell_name": "pshare50"}
    del parameters[absent]

    with pytest.raises(CacheKeyError, match=absent):
        get_cell_cache_key(None, parameters)
