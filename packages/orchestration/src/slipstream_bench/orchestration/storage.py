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

from typing import Any

from prefect_aws import S3Bucket

_RESULT_STORAGE_PREFIX = "prefect/results"
_CACHE_KEY_STORAGE_PREFIX = "prefect/cache-keys"

# The object the preflight writes and reads back under the cache-key prefix, and its body.
_PREFLIGHT_PROBE_KEY = "preflight-probe"
_PREFLIGHT_CONTENT = b"slipstream cache-key storage preflight"


class StorageError(Exception):
    """A storage configuration that cannot point Prefect at S3."""


def result_storage(bucket: str) -> S3Bucket:
    """Build the S3 block Prefect persists task results into.

    :param bucket: the results bucket (``RESULTS_BUCKET``), shared with the sweep's
        own ``s3://<bucket>/sweeps/…`` output under a separate prefix.
    :return: an :class:`~prefect_aws.S3Bucket` rooted at the result prefix.
    :raise StorageError: when ``bucket`` is blank.
    """
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
    return S3Bucket(
        bucket_name=_require_bucket(bucket), bucket_folder=_CACHE_KEY_STORAGE_PREFIX
    )


def check_cache_key_storage(cache_keys: Any, *, bucket: str) -> None:
    """Write a probe object to the cache-key storage and read it back, or fail loud.

    Prefect's cache existence check reads any storage error as "not cached", so a
    missing bucket or a denied grant would make every cell look pending and trigger a
    ~20-minute GPU redeploy for nothing. Probing at flow start turns that blind spot
    into an abort before the first redeploy. The probe overwrites one fixed object, so
    repeated runs leave no litter and cannot collide with a ``digest:slug:cell`` key.

    :param cache_keys: the block Prefect stores cache records into; its
        ``write_path``/``read_path`` dispatch synchronously when called from sync code
        such as the flow body. Tests pass a tmp ``LocalFileSystem``; production passes
        the S3 block.
    :param bucket: the results bucket ``cache_keys`` is rooted in, named in the error.
    :raise StorageError: when the probe cannot be written or read back.
    """
    location = f"s3://{bucket}/{_CACHE_KEY_STORAGE_PREFIX}"
    fix = (
        "check the bucket name and that the worker's IAM role grants read and write "
        "on that prefix"
    )
    try:
        cache_keys.write_path(_PREFLIGHT_PROBE_KEY, _PREFLIGHT_CONTENT)
        cache_keys.read_path(_PREFLIGHT_PROBE_KEY)
    except Exception as exc:  # any S3 or filesystem failure means the cache is unusable
        raise StorageError(
            f"cache-key storage at {location} failed its write/read probe "
            f"({type(exc).__name__}: {exc}); {fix}"
        ) from exc


def _require_bucket(bucket: str) -> str:
    """Reject a blank bucket name at any builder entry.

    The bucket's existence and permissions are not checked here;
    :func:`check_cache_key_storage` probes them once the flow has registered the
    storage blocks.
    """
    name = bucket.strip()
    if not name:
        raise StorageError(
            "bucket name is blank: Prefect result and cache-key storage must point "
            "at an S3 bucket, not the local ~/.prefect/storage/ default"
        )
    return name
