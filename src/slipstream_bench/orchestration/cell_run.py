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
import functools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import yaml

from slipstream_bench.orchestration.cell_task import CellExecution
from slipstream_bench.orchestration.ssm import run_command
from slipstream_bench.sweep.config import RESERVED_KEYS, CellConfig
from slipstream_bench.sweep.runner import get_cell_basename

# The host script the orchestration layer runs per cell (dropped at boot on the bench
# host). It reads the cell env this module prefixes and runs one docker cell.
_BENCH_CELL_SCRIPT = "/usr/local/bin/bench-sweep.sh"


class CellResultError(Exception):
    """A cell that ran over SSM but whose result object could not be downloaded."""


def render_cell_config(cell: CellConfig) -> str:
    """Render one cell to the ``load-cell`` config YAML, minus the CLI-injected keys.

    ``base_url``, ``model``, and ``out_dir`` are supplied by ``bench-sweep.sh`` as
    flags; ``commercial`` is a CLI-derived context key (from ``--api-key-env``), not a
    config field. All four are RESERVED context keys ``load_cell_config`` rejects if
    present in the file (model.yaml is the served-model source of truth), so they are
    excluded here.

    :param cell: the cell to render.
    :return: the YAML text to base64 and ship as ``bench-sweep.sh``'s ``CELL_CONFIG_B64``.
    """
    knobs = cell.model_dump(exclude=set(RESERVED_KEYS))
    return yaml.safe_dump(knobs, sort_keys=True)


def get_cell_s3_key(run_id: str, cell: CellConfig) -> str:
    """Address a cell's result object under the run's sweep prefix.

    ``bench-sweep.sh`` copies the cell JSON to ``s3://<bucket>/sweeps/<run_id>/``;
    ``run_id`` already nests the engine point (``<run>/<point-slug>``) for a knob sweep.

    :param run_id: the run's bucket prefix, point-nested by the caller.
    :param cell: the cell whose object is addressed.
    :return: the object key, without the bucket.
    """
    return f"sweeps/{run_id}/{get_cell_basename(cell)}"


def get_cell_result_uri(bucket: str, run_id: str, cell: CellConfig) -> str:
    """Build the ``s3://`` pointer the cell task returns as the single source of truth.

    :param bucket: the results bucket.
    :param run_id: the run's bucket prefix, point-nested by the caller.
    :param cell: the cell whose object is addressed.
    :return: the fully-qualified ``s3://`` URI.
    """
    return f"s3://{bucket}/{get_cell_s3_key(run_id, cell)}"


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
    line intact. ``SWEEP_ARGS_B64`` is appended only when set, matching the host
    script's optional read. The tokenizer the offline cell synthesises against is the
    image's baked snapshot: the image names its path, so no env carries it here.

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


@dataclass(frozen=True)
class CellExecutionContext:
    """The fixed context a point's cells run against: clients, host, run addressing.

    Every field is constant across one point sweep — only the cell and its local
    destination vary — so the flow builds one instance and reuses it for every cell,
    instead of threading a dozen identical arguments through :func:`build_cell_execution`
    each iteration.

    :param ssm_client: the boto3 SSM client (or stand-in) commands are sent through.
    :param s3_client: the boto3 S3 client (or stand-in) results are downloaded with.
    :param instance_id: the bench host running the cells.
    :param image_ref: the bench-client image the cells run.
    :param bucket: the results bucket the cells write to and are downloaded from.
    :param model: the served model id the cells measure.
    :param run_id: the run's bucket prefix, point-nested by the caller.
    :param sweep_args_b64: optional extra ``load-cell`` flags, base64-encoded.
    :param timeout_s: the ceiling on each cell's SSM command.
    :param poll_interval_s: the wait between SSM invocation polls.
    :param sleep: the wait function, injected for tests.
    """

    ssm_client: Any
    s3_client: Any
    instance_id: str
    image_ref: str
    bucket: str
    model: str
    run_id: str
    sweep_args_b64: str = ""
    timeout_s: float = 3600.0
    poll_interval_s: float = 5.0
    sleep: Callable[[float], None] | None = None


def build_cell_execution(
    cell: CellConfig, *, context: CellExecutionContext, dest: Path
) -> CellExecution:
    """Build the callable the cell task runs: SSM the cell, then download its result.

    :param cell: the cell to run.
    :param context: the fixed per-run context (clients, host, run addressing, timing).
    :param dest: the local path the result is downloaded to (the gate's input).
    :return: the :data:`CellExecution` the cell task wraps.
    """
    config_b64 = base64.b64encode(render_cell_config(cell).encode("utf-8")).decode(
        "ascii"
    )
    command = build_cell_command(
        image_ref=context.image_ref,
        bucket=context.bucket,
        model=context.model,
        run_id=context.run_id,
        cell_config_b64=config_b64,
        sweep_args_b64=context.sweep_args_b64,
    )
    return functools.partial(
        _run_and_download,
        context=context,
        command=command,
        key=get_cell_s3_key(context.run_id, cell),
        dest=dest,
    )


def build_s3_client(region: str) -> Any:
    """Build a boto3 S3 client for ``region`` from the ambient credential chain.

    :param region: the AWS region the results bucket lives in.
    :return: a boto3 S3 client.
    """
    return boto3.client("s3", region_name=region)


def _run_and_download(
    *,
    context: CellExecutionContext,
    command: str,
    key: str,
    dest: Path,
) -> None:
    """Send one cell over SSM, then download its result JSON to ``dest``.

    Sends ``bench-sweep.sh`` (which runs one ``docker run`` against the once-per-flow
    loopback proxy and copies the result to S3), then downloads that result object so
    the validity gate reads it locally. Any SSM failure raises, so the cell task never
    caches it; a missing result object raises :class:`CellResultError`.
    """
    run_kwargs: dict[str, Any] = {
        "instance_id": context.instance_id,
        "command": command,
        "timeout_s": context.timeout_s,
        "poll_interval_s": context.poll_interval_s,
    }
    if context.sleep is not None:
        run_kwargs["sleep"] = context.sleep
    run_command(context.ssm_client, **run_kwargs)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        context.s3_client.download_file(context.bucket, key, str(dest))
    except Exception as error:
        raise CellResultError(
            f"the cell ran on {context.instance_id} but its result object "
            f"s3://{context.bucket}/{key} could not be downloaded to {dest}: {error}"
        ) from error
