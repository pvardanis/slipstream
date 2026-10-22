"""Build a kv-cache-extension-ab ladder rung's cell, its S3 addresses, and its digest.

Each rung of an A/B run is one cell of the workload's load knobs at its prefix share and
the rung's concurrency. ``bench-sweep.sh`` copies every raw cell result to
``<RUN_ID>/``, so a run's cells run under the run id ``kv-offload-ab/<run>/<arm>-r<n>``:
their raw results land under ``kv-offload-ab/``, apart from any knob sweep's, and the
labels the worker writes land beside each raw ``<cell>.json`` as ``<cell>.kv_ab.json``,
so the raw result is stored once. The digest
folds in the engine args both arms deploy, the rig's capacities, and the cell's knobs,
so a cached rung is re-measured when any changes; the arm and run are cache-key parts
of their own.
The worker reads the engine's /metrics over the cluster network around each rung
(:func:`fetch_metrics`) and writes each rung's labels to S3 (:func:`write_kv_ab_labels`).
"""

import http.client
import json
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from slipstream_bench.contract import CellConfig
from slipstream_bench.orchestration.cell_run import (
    PLACEHOLDER_BASE_URL,
    PLACEHOLDER_OUT_DIR,
    get_cell_knobs,
)
from slipstream_bench.orchestration.digest import ServingConfig, config_digest
from slipstream_bench.orchestration.kv_ab_config import (
    KvAbConfig,
    get_kv_ab_engine_args,
)
from slipstream_bench.orchestration.kv_ab_rig import KvAbRig
from slipstream_bench.orchestration.validity import DEFAULT_MAX_ERROR_RATE

# Poisson arrivals, the knob sweep's pinned burstiness. The workload's request rate is
# unbounded, so the concurrency cap alone paces the rung.
_BURSTINESS = 1.0

# The prefix every A/B run's cells, labels, and charts nest under.
_KV_AB_PREFIX = "kv-offload-ab"

# The suffixes of a rung's raw result object and of the labels object beside it.
_RESULT_SUFFIX = ".json"
_LABELS_SUFFIX = ".kv_ab.json"


class MetricsError(Exception):
    """A serving engine whose /metrics could not be read."""


def get_kv_ab_cell(config: KvAbConfig, concurrency: int, *, model: str) -> CellConfig:
    """Build the cell one rung runs: the workload's load at the rung's concurrency.

    :param config: the A/B workload.
    :param concurrency: the rung's in-flight request cap.
    :param model: the served model id the cell measures.
    :return: the rung's cell.
    """
    return CellConfig(
        **config.load.model_dump(),
        base_url=PLACEHOLDER_BASE_URL,
        model=model,
        out_dir=PLACEHOLDER_OUT_DIR,
        prefix_share=config.prefix_share,
        burstiness=_BURSTINESS,
        max_concurrency=concurrency,
    )


def get_kv_ab_run_prefix(run: str) -> str:
    """Name the run-id prefix an A/B's arm runs nest under: ``kv-offload-ab/<run>``.

    The bench host's raw results, the rungs' labels beside them, and the run's chart all
    land under ``kv-offload-ab/<run>/`` at the bucket root.

    :param run: the A/B's run id.
    :return: the prefix, without a trailing slash.
    """
    return f"{_KV_AB_PREFIX}/{run}"


def get_kv_ab_run_id(run: str, arm_run_slug: str) -> str:
    """Name the run id one A/B run's cells run under: ``kv-offload-ab/<run>/<arm>-r<n>``.

    :param run: the A/B's run id.
    :param arm_run_slug: the arm run's slug, e.g. ``on-r2``.
    :return: the bench host's ``RUN_ID`` and the cells' cache-key run id.
    """
    return f"{get_kv_ab_run_prefix(run)}/{arm_run_slug}"


def get_kv_ab_label_key(result_key: str) -> str:
    """Address a rung's labels beside its raw result: ``<cell>.json`` to ``<cell>.kv_ab.json``.

    :param result_key: the rung's raw result object key (``get_cell_s3_key``).
    :return: the labels' object key, without the bucket.
    :raise ValueError: when ``result_key`` is not a .json object key.
    """
    return _swap_suffix(
        result_key, old=_RESULT_SUFFIX, new=_LABELS_SUFFIX, what="result"
    )


def get_kv_ab_result_key(label_key: str) -> str:
    """Address a rung's raw result from its labels: ``<cell>.kv_ab.json`` to ``<cell>.json``.

    :param label_key: the rung's labels object key (:func:`get_kv_ab_label_key`).
    :return: the raw result's object key, without the bucket.
    :raise ValueError: when ``label_key`` is not a .kv_ab.json object key.
    """
    return _swap_suffix(
        label_key, old=_LABELS_SUFFIX, new=_RESULT_SUFFIX, what="labels"
    )


def _swap_suffix(key: str, *, old: str, new: str, what: str) -> str:
    """Swap a rung object key's ``old`` suffix for ``new``, refusing a key without it."""
    if not key.endswith(old):
        raise ValueError(f"{what} key {key} is not a {old} object key")
    return f"{key.removesuffix(old)}{new}"


def get_kv_ab_cell_digest(
    config: KvAbConfig,
    cell: CellConfig,
    *,
    serving: ServingConfig,
    image_ref: str,
    rig: KvAbRig,
) -> str:
    """Compute a rung's config digest, the cache-key part naming its config identity.

    :param config: the A/B workload, whose engine args both arms deploy.
    :param cell: the rung's cell.
    :param serving: the run-wide model and vLLM serving image.
    :param image_ref: the bench-client image the cell runs.
    :param rig: the rig capacities, the CPU tier among them, the on arm offloads to.
    :return: the cell's 64-char hex config digest.
    """
    return config_digest(
        model_yaml=serving.model_yaml,
        point_knobs={
            **asdict(get_kv_ab_engine_args(config.engine)),
            "rig": asdict(rig),
        },
        cell_params=get_cell_knobs(cell),
        vllm_image_ref=serving.vllm_image_ref,
        bench_image_ref=image_ref,
        sweep_flags=(),
        max_error_rate=DEFAULT_MAX_ERROR_RATE,
    )


def fetch_metrics(url: str, *, timeout_s: float = 10.0) -> str:
    """Read the serving engine's Prometheus /metrics exposition text.

    :param url: the engine's /metrics URL, e.g.
        ``http://vllm-gpu.slipstream.svc:8000/metrics``.
    :param timeout_s: the ceiling on the request.
    :return: the exposition text.
    :raise MetricsError: when the URL is not http(s), the request or the body read
        fails, or the engine answers with an error.
    """
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise MetricsError(
            f"the engine's metrics URL {url} is not http or https: pass the vLLM "
            f"Service's /metrics URL"
        )
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            return response.read().decode("utf-8")
    # OSError covers the connection, the HTTP error status (URLError), and the timeout;
    # HTTPException and UnicodeDecodeError cover a body cut short or not text.
    except (OSError, http.client.HTTPException, UnicodeDecodeError) as error:
        raise MetricsError(
            f"could not read the engine's metrics at {url}: {error}: is the vLLM "
            f"Service up and reachable from the worker?"
        ) from error


def write_kv_ab_labels(
    labels: Mapping[str, Any], *, s3_client: Any, bucket: str, key: str
) -> str:
    """Write a rung's labels to S3 as JSON and return their pointer.

    :param labels: the rung's labels.
    :param s3_client: the boto3 S3 client (or stand-in) the labels are written with.
    :param bucket: the results bucket.
    :param key: the labels' object key (:func:`get_kv_ab_label_key`).
    :return: the labels' ``s3://`` URI.
    """
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(labels).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{key}"
