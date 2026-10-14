"""The Prefect result and cache-key storage resolve to S3, never the local default.

ADR-0012 makes resume hinge on one fact: ``result_storage`` and ``key_storage``
must point at S3, not Prefect's ``~/.prefect/storage/`` default, and the two must be
distinct prefixes so the cell JSON and the cache index never collide. These tests
pin that fact and the preflight that proves the cache-key storage is writable and
readable before a sweep trusts it.
"""

from pathlib import Path

import pytest
from prefect.filesystems import LocalFileSystem
from prefect_aws import S3Bucket

from slipstream_bench.orchestration.storage import (
    StorageError,
    cache_key_storage,
    check_cache_key_storage,
    result_storage,
)

_BUCKET = "slipstream-bench-results"


def test_result_storage_is_an_s3_bucket_at_its_own_prefix() -> None:
    storage = result_storage(_BUCKET)

    assert isinstance(storage, S3Bucket)
    assert storage.bucket_name == _BUCKET
    assert storage.bucket_folder == "prefect/results"


def test_cache_key_storage_is_an_s3_bucket_at_its_own_prefix() -> None:
    storage = cache_key_storage(_BUCKET)

    assert isinstance(storage, S3Bucket)
    assert storage.bucket_name == _BUCKET
    assert storage.bucket_folder == "prefect/cache-keys"


def test_result_and_cache_key_prefixes_do_not_collide() -> None:
    assert (
        result_storage(_BUCKET).bucket_folder
        != cache_key_storage(_BUCKET).bucket_folder
    )


@pytest.mark.parametrize("build", [result_storage, cache_key_storage])
def test_builders_reject_a_blank_bucket(build) -> None:
    with pytest.raises(StorageError, match="blank"):
        build("   ")


def test_result_storage_trims_surrounding_whitespace() -> None:
    assert result_storage(f"  {_BUCKET}  ").bucket_name == _BUCKET


def test_check_cache_key_storage_passes_on_writable_storage(tmp_path: Path) -> None:
    check_cache_key_storage(
        LocalFileSystem(basepath=str(tmp_path / "cache-keys")), bucket=_BUCKET
    )


def test_check_cache_key_storage_names_the_bucket_and_prefix_on_failure(
    tmp_path: Path,
) -> None:
    # A regular file where the storage root should be: the probe write cannot land, as
    # with a missing bucket or a denied IAM grant on S3.
    root = tmp_path / "not-a-dir"
    root.write_text("", encoding="utf-8")

    with pytest.raises(StorageError) as raised:
        check_cache_key_storage(LocalFileSystem(basepath=str(root)), bucket=_BUCKET)

    message = str(raised.value)
    assert f"s3://{_BUCKET}/prefect/cache-keys" in message
    assert "IAM" in message
    assert isinstance(raised.value.__cause__, OSError)
