"""Point Prefect's result and cache-key storage at S3, the hinge of sweep resume.

ADR-0012: resume survives a server or laptop death only if ``result_storage`` and
``key_storage`` live in S3, not Prefect's local ``~/.prefect/storage/`` default. The
per-cell JSON (the numbers) and Prefect's cache/key index (pointers + metadata) are
two distinct, non-duplicating prefixes in the same results bucket the sweep already
writes to (``RESULTS_BUCKET``, ``s3://<bucket>/sweeps/…``). These builders return the
S3 blocks the later sweep task binds to ``result_storage`` and a cache policy's
``key_storage``; AWS credentials come from the ambient default chain, as the ``aws``
CLI already uses in ``just bench``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prefect_aws import S3Bucket

_RESULTS_BUCKET_ENV = "RESULTS_BUCKET"
_RESULT_STORAGE_PREFIX = "prefect/results"
_CACHE_KEY_STORAGE_PREFIX = "prefect/cache-keys"


class StorageError(Exception):
    """A storage configuration that cannot point Prefect at S3."""


def _require_bucket(bucket: str) -> str:
    """Reject a blank bucket name at any builder entry, not only the env reader.

    The bucket's existence and write permissions are not checked here; those
    resolve at first write, when Prefect persists a result to S3.
    """
    name = bucket.strip()
    if not name:
        raise StorageError(
            "bucket name is blank: Prefect result and cache-key storage must point "
            "at an S3 bucket, not the local ~/.prefect/storage/ default"
        )
    return name


def result_storage(bucket: str) -> S3Bucket:
    """Build the S3 block Prefect persists task results into.

    :param bucket: the results bucket (``RESULTS_BUCKET``), shared with the sweep's
        own ``s3://<bucket>/sweeps/…`` output under a separate prefix.
    :return: an :class:`~prefect_aws.S3Bucket` rooted at the result prefix.
    :raise StorageError: when ``bucket`` is blank.
    """
    from prefect_aws import S3Bucket

    return S3Bucket(
        bucket_name=_require_bucket(bucket), bucket_folder=_RESULT_STORAGE_PREFIX
    )


def cache_key_storage(bucket: str) -> S3Bucket:
    """Build the S3 block Prefect stores cache records (keys) into.

    Kept a distinct prefix from :func:`result_storage` so a cache record can be
    deleted or rewritten without touching the persisted result it points at.

    :param bucket: the results bucket (``RESULTS_BUCKET``).
    :return: an :class:`~prefect_aws.S3Bucket` rooted at the cache-key prefix.
    :raise StorageError: when ``bucket`` is blank.
    """
    from prefect_aws import S3Bucket

    return S3Bucket(
        bucket_name=_require_bucket(bucket), bucket_folder=_CACHE_KEY_STORAGE_PREFIX
    )


def results_bucket_from_env() -> str:
    """Read the results bucket the sweep runs against from the environment.

    ``just bench`` injects ``RESULTS_BUCKET`` (the terraform ``results_bucket_name``)
    into the sweep's environment; this is the one place that env var crosses into the
    storage config.

    :return: the bucket name.
    :raise StorageError: when ``RESULTS_BUCKET`` is unset or blank.
    """
    bucket = os.environ.get(_RESULTS_BUCKET_ENV, "").strip()
    if not bucket:
        raise StorageError(
            f"{_RESULTS_BUCKET_ENV} is unset or blank: Prefect result and cache-key "
            "storage must point at S3, not the local ~/.prefect/storage/ default; "
            "set it to the sweep results bucket (terraform results_bucket_name)"
        )
    return bucket
