"""Turn one ``CellConfig`` into the ``execute_func`` the cell task runs over SSM.

ADR-0012 §Amendment: the per-cell loop lives here, and each cell is one ``docker run``
on the bench host. This module renders a cell to the YAML ``load-cell`` reads (the
CLI-injected keys excluded, so ``bench-sweep.sh`` supplies ``--base-url``/``--model``/
``--out-dir`` as flags), addresses the cell's result object under
``sweeps/<run_id>/<basename>``, and builds the :data:`CellExecution` closure the cell
task wraps: send ``bench-sweep.sh`` over SSM, then download the result JSON so the
validity gate reads it locally. The transport (:mod:`slipstream_bench.orchestration.ssm`)
and the S3 download client are passed in, so this stays a thin composition over them.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from slipstream_bench.orchestration.cell_task import CellExecution
from slipstream_bench.orchestration.ssm import run_command
from slipstream_bench.sweep.config import RESERVED_KEYS, CellConfig
from slipstream_bench.sweep.runner import cell_basename

# The host script the orchestration layer runs per cell (dropped at boot on the bench
# host). It reads the cell env this module prefixes and runs one docker cell.
_BENCH_CELL_SCRIPT = "/usr/local/bin/bench-sweep.sh"


def render_cell_config(cell: CellConfig) -> str:
    """Render one cell to the ``load-cell`` config YAML, minus the CLI-injected keys.

    ``base_url``, ``model``, ``out_dir``, and ``commercial`` are supplied by
    ``bench-sweep.sh`` as flags and rejected by ``load_cell_config`` if present in the
    file (model.yaml is the served-model source of truth), so they are excluded here.

    :param cell: the cell to render.
    :return: the YAML text to base64 and ship as ``bench-sweep.sh``'s ``CELL_CONFIG_B64``.
    """
    knobs = cell.model_dump(exclude=set(RESERVED_KEYS))
    return yaml.safe_dump(knobs, sort_keys=True)


def cell_s3_key(run_id: str, cell: CellConfig) -> str:
    """Address a cell's result object under the run's sweep prefix.

    ``bench-sweep.sh`` copies the cell JSON to ``s3://<bucket>/sweeps/<run_id>/``;
    ``run_id`` already nests the engine point (``<run>/<point-slug>``) for a knob sweep.

    :param run_id: the run's bucket prefix, point-nested by the caller.
    :param cell: the cell whose object is addressed.
    :return: the object key, without the bucket.
    """
    return f"sweeps/{run_id}/{cell_basename(cell)}"


def cell_result_uri(bucket: str, run_id: str, cell: CellConfig) -> str:
    """Build the ``s3://`` pointer the cell task returns as the single source of truth.

    :param bucket: the results bucket.
    :param run_id: the run's bucket prefix, point-nested by the caller.
    :param cell: the cell whose object is addressed.
    :return: the fully-qualified ``s3://`` URI.
    """
    return f"s3://{bucket}/{cell_s3_key(run_id, cell)}"


def build_cell_command(
    *,
    image_ref: str,
    bucket: str,
    model: str,
    run_id: str,
    cell_config_b64: str,
    sweep_args_b64: str = "",
) -> str:
    """Compose the SSM command line: the cell env prefixed to ``bench-sweep.sh``.

    The values carry no shell-special characters (an image ref, a bucket, a model id,
    a run prefix, base64), so single-quoting each is enough to cross the SSM command
    line intact. ``SWEEP_ARGS_B64`` is appended only when non-empty, matching the
    host script's optional read.

    :return: the shell command line the SSM transport sends to the bench host.
    """
    env = [
        f"IMAGE_REF='{image_ref}'",
        f"RESULTS_BUCKET='{bucket}'",
        f"MODEL='{model}'",
        f"RUN_ID='{run_id}'",
        f"CELL_CONFIG_B64='{cell_config_b64}'",
    ]
    if sweep_args_b64:
        env.append(f"SWEEP_ARGS_B64='{sweep_args_b64}'")
    return " ".join([*env, _BENCH_CELL_SCRIPT])


def build_cell_execution(
    *,
    cell: CellConfig,
    ssm_client: Any,
    s3_client: Any,
    instance_id: str,
    image_ref: str,
    bucket: str,
    model: str,
    run_id: str,
    dest: Path,
    sweep_args_b64: str = "",
    timeout_s: float = 3600.0,
    poll_interval_s: float = 5.0,
    sleep: Callable[[float], None] | None = None,
) -> CellExecution:
    """Build the closure the cell task runs: SSM the cell, then download its result.

    The closure sends ``bench-sweep.sh`` (which runs one ``docker run`` against the
    once-per-flow loopback proxy and copies the result to S3) over SSM, then downloads
    that result object to ``dest`` so the validity gate reads it locally. Any SSM
    failure raises out of the closure, so the cell task never caches it.

    :param cell: the cell to run.
    :param ssm_client: the boto3 SSM client (or stand-in) the command is sent through.
    :param s3_client: the boto3 S3 client (or stand-in) the result is downloaded with.
    :param instance_id: the bench host running the cell.
    :param image_ref: the bench-client image the cell runs.
    :param bucket: the results bucket the cell writes to and is downloaded from.
    :param model: the served model id the cell measures.
    :param run_id: the run's bucket prefix, point-nested by the caller.
    :param dest: the local path the result is downloaded to (the gate's input).
    :param sweep_args_b64: optional extra ``load-cell`` flags, base64-encoded.
    :param timeout_s: the ceiling on the SSM command.
    :param poll_interval_s: the wait between SSM invocation polls.
    :param sleep: the wait function, injected for tests.
    :return: the :data:`CellExecution` the cell task wraps.
    """
    config_b64 = base64.b64encode(render_cell_config(cell).encode("utf-8")).decode(
        "ascii"
    )
    command = build_cell_command(
        image_ref=image_ref,
        bucket=bucket,
        model=model,
        run_id=run_id,
        cell_config_b64=config_b64,
        sweep_args_b64=sweep_args_b64,
    )
    key = cell_s3_key(run_id, cell)

    def execute() -> None:
        run_kwargs: dict[str, Any] = {
            "instance_id": instance_id,
            "command": command,
            "timeout_s": timeout_s,
            "poll_interval_s": poll_interval_s,
        }
        if sleep is not None:
            run_kwargs["sleep"] = sleep
        run_command(ssm_client, **run_kwargs)
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3_client.download_file(bucket, key, str(dest))

    return execute


def build_s3_client(region: str) -> Any:
    """Build a boto3 S3 client for ``region`` from the ambient credential chain.

    :param region: the AWS region the results bucket lives in.
    :return: a boto3 S3 client.
    :raise ImportError: when ``boto3`` (the ``orchestration`` extra) is not installed,
        re-raised naming the extra to install, matching the SSM client builder.
    """
    try:
        import boto3
    except ImportError as error:
        raise ImportError(
            "boto3 is not installed: downloading a cell result needs the "
            "'orchestration' extra (pip install 'slipstream-bench[orchestration]')"
        ) from error
    return boto3.client("s3", region_name=region)
