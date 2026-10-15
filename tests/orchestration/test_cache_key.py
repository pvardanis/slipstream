"""The cache key is ``run-id:digest:point-slug:cell-name`` — the address of a cell.

A Prefect ``cache_key_fn`` returns ``run-id:digest:point-slug:cell-name``; on a hit the
task enters ``Cached`` and does not run. The run id scopes reuse to one run (a new run
id re-measures, the same one resumes), the digest carries config identity, the point
slug the engine coordinates, and the cell name the client-load rung, so the four
together address exactly one measurement. The pure builder is tested without Prefect;
``get_cell_cache_key`` reads the same four off a task's call parameters.
"""

import pytest

from slipstream_bench.orchestration.cache_key import (
    CacheKeyError,
    build_cache_key,
    get_cell_cache_key,
)

_PARTS = {
    "run_id": "run1",
    "digest": "abc123",
    "point_slug": "mns64_kvfp8_pcon",
    "cell_name": "pshare50_mc64",
}


def test_build_cache_key_joins_the_four_parts() -> None:
    key = build_cache_key(**_PARTS)

    assert key == "run1:abc123:mns64_kvfp8_pcon:pshare50_mc64"


def test_cells_of_different_runs_have_different_keys() -> None:
    assert build_cache_key(**_PARTS) != build_cache_key(**{**_PARTS, "run_id": "run2"})


def test_get_cell_cache_key_reads_the_parts_off_the_call_parameters() -> None:
    parameters = {**_PARTS, "result_uri": "s3://bucket/sweeps/run/cell.json"}

    assert (
        get_cell_cache_key(None, parameters)
        == "run1:abc123:mns64_kvfp8_pcon:pshare50_mc64"
    )


@pytest.mark.parametrize("blank", list(_PARTS))
def test_a_blank_part_is_rejected(blank: str) -> None:
    parts = {**_PARTS, blank: "  "}

    with pytest.raises(CacheKeyError, match=blank):
        build_cache_key(**parts)


@pytest.mark.parametrize("absent", list(_PARTS))
def test_get_cell_cache_key_rejects_an_absent_call_parameter(absent: str) -> None:
    parameters = dict(_PARTS)
    del parameters[absent]

    with pytest.raises(CacheKeyError, match=absent):
        get_cell_cache_key(None, parameters)
