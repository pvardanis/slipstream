"""Build a kv-cache-extension-ab ladder rung's cell, its S3 addresses, and its digest.

Each rung of an A/B run is one cell of the workload's load knobs at its prefix share and
the rung's concurrency. ``bench-sweep.sh`` copies every raw cell result to
``sweeps/<RUN_ID>/``, so a run's cells run under the run id ``kv-ab/<run>/<arm>-r<n>``:
their raw results land under ``sweeps/kv-ab/``, apart from any knob sweep's, and the
labelled record the worker writes lands at the same path under ``kv-ab/``. The digest
folds in the engine args both arms deploy and the cell's knobs, so a cached rung is
re-measured when either changes; the arm and run are cache-key parts of their own.
"""

from dataclasses import asdict

from slipstream_bench.contract import CellConfig, get_cell_basename
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
from slipstream_bench.orchestration.validity import DEFAULT_MAX_ERROR_RATE

# Poisson arrivals, the knob sweep's pinned burstiness. The workload's request rate is
# unbounded, so the concurrency cap alone paces the rung.
_BURSTINESS = 1.0

# The prefix every A/B run's cells and records nest under.
_KV_AB_PREFIX = "kv-ab"


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


def get_kv_ab_run_id(run: str, arm_run_slug: str) -> str:
    """Name the run id one A/B run's cells run under: ``kv-ab/<run>/<arm>-r<n>``.

    :param run: the A/B's run id.
    :param arm_run_slug: the arm run's slug, e.g. ``on-r2``.
    :return: the bench host's ``RUN_ID`` and the cells' cache-key run id.
    """
    return f"{_KV_AB_PREFIX}/{run}/{arm_run_slug}"


def get_kv_ab_record_key(run: str, arm_run_slug: str, cell: CellConfig) -> str:
    """Address a rung's labelled record: ``kv-ab/<run>/<arm>-r<n>/<cell>.json``.

    :param run: the A/B's run id.
    :param arm_run_slug: the arm run's slug, e.g. ``on-r2``.
    :param cell: the rung's cell.
    :return: the object key, without the bucket.
    """
    return f"{get_kv_ab_run_id(run, arm_run_slug)}/{get_cell_basename(cell)}"


def get_kv_ab_cell_digest(
    config: KvAbConfig, cell: CellConfig, *, serving: ServingConfig, image_ref: str
) -> str:
    """Compute a rung's config digest, the cache-key part naming its config identity.

    :param config: the A/B workload, whose engine args both arms deploy.
    :param cell: the rung's cell.
    :param serving: the run-wide model and vLLM serving image.
    :param image_ref: the bench-client image the cell runs.
    :return: the cell's 64-char hex config digest.
    """
    return config_digest(
        model_yaml=serving.model_yaml,
        point_knobs=asdict(get_kv_ab_engine_args(config.engine)),
        cell_params=get_cell_knobs(cell),
        vllm_image_ref=serving.vllm_image_ref,
        bench_image_ref=image_ref,
        sweep_flags=(),
        max_error_rate=DEFAULT_MAX_ERROR_RATE,
    )
