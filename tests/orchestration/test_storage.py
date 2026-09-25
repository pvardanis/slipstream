"""The Prefect result and cache-key storage resolve to S3, never the local default.

ADR-0012 makes resume hinge on one fact: ``result_storage`` and ``key_storage``
must point at S3, not Prefect's ``~/.prefect/storage/`` default, and the two must be
distinct prefixes so the cell JSON and the cache index never collide. These tests
pin that fact and the env-var boundary that supplies the bucket.
"""

import sys

import pytest
from prefect_aws import S3Bucket

from slipstream_bench.orchestration.storage import (
    StorageError,
    cache_key_storage,
    result_storage,
    results_bucket_from_env,
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


@pytest.mark.parametrize("build", [result_storage, cache_key_storage])
def test_builders_report_the_missing_orchestration_extra(
    build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build without ``prefect_aws`` names the extra to install, not a bare import."""
    monkeypatch.setitem(sys.modules, "prefect_aws", None)

    with pytest.raises(StorageError, match="orchestration"):
        build(_BUCKET)


def test_results_bucket_from_env_reads_the_sweep_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESULTS_BUCKET", _BUCKET)

    assert results_bucket_from_env() == _BUCKET


def test_results_bucket_from_env_trims_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESULTS_BUCKET", f"  {_BUCKET}  ")

    assert results_bucket_from_env() == _BUCKET


def test_results_bucket_from_env_rejects_a_missing_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESULTS_BUCKET", raising=False)

    with pytest.raises(StorageError, match="RESULTS_BUCKET"):
        results_bucket_from_env()


def test_results_bucket_from_env_rejects_a_blank_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESULTS_BUCKET", "   ")

    with pytest.raises(StorageError, match="RESULTS_BUCKET"):
        results_bucket_from_env()
