"""Set up a parent flow run: milestone logs, the run's group tag, and its bench endpoint.

The knob sweep and the KV cache offload A/B call all three at their composition roots, so
the Prefect-free core's INFO milestones reach the UI, the run joins the ``run=<run-id>``
filter, and its cells run on a bench endpoint opened from the worker's environment.
"""

import logging
import os
from contextlib import AbstractContextManager
from typing import Any

from prefect.client.orchestration import get_client
from prefect.runtime import flow_run

from slipstream_bench.orchestration.bench_endpoint import (
    BenchHost,
    build_terraform,
    open_bench_endpoint,
    read_stack_dir,
    read_topology,
)
from slipstream_bench.orchestration.cluster import Kubectl, read_vllm_api_key

# The package whose stdlib loggers carry the Prefect-free core's milestone lines. The
# image's PREFECT_LOGGING_EXTRA_LOGGERS=slipstream_bench attaches a handler to this logger
# but sets no level, so it inherits root's WARNING default and the INFO milestones are
# filtered before the handler sees them.
_PACKAGE_LOGGER = "slipstream_bench"


def enable_milestone_logging() -> None:
    """Lift the package logger to INFO so the core's stdlib milestones reach the UI.

    ``PREFECT_LOGGING_EXTRA_LOGGERS=slipstream_bench`` wires the API log handler onto the
    package logger but leaves its level unset, so the core's ``_LOGGER.info`` milestones
    (cell start/done, point done, ceiling scrape) stay below the inherited WARNING threshold
    and never reach that handler. Each flow calls this at its composition root — the Prefect
    layer, not the Prefect-free core — to drop the threshold to INFO so those lines surface.
    """
    logging.getLogger(_PACKAGE_LOGGER).setLevel(logging.INFO)


def tag_run_with_group(run_id: str) -> None:
    """Tag the running flow run ``run=<run-id>``, the group the UI filters a run by.

    The knob sweep's point sweeps tag their own runs through the ``tags`` context manager
    (:func:`slipstream_bench.orchestration.flows.point_sweep.run_point_sweep`); a parent
    run is already created when its flow body runs, so that context manager cannot reach it
    — its run is updated through the API instead. The update overwrites tags rather than
    merging, so the current set is read and the run tag added to it. A direct call outside a
    flow run (no runtime id) is a no-op.
    """
    flow_run_id = flow_run.id
    if flow_run_id is None:
        return
    with get_client(sync_client=True) as client:
        client.update_flow_run(flow_run_id, tags=set(flow_run.tags) | {f"run={run_id}"})


def open_run_bench_endpoint(
    *, kubectl: Kubectl, s3_client: Any, bucket: str
) -> AbstractContextManager[BenchHost]:
    """Open the run's bench endpoint from the worker's environment.

    The topology and the baked stack's path come from the worker's environment, the vLLM
    api-key from its cluster Secret, and the bench host reports its boot to the results
    bucket the cells write to.

    :param kubectl: the transport the api-key Secret is read through.
    :param s3_client: the boto3 S3 client the boot sentinel is read with.
    :param bucket: the results bucket the bench host reports its boot to.
    :return: the session, which yields the bench host and destroys the endpoint on exit.
    :raise BenchEndpointEnvironmentError: when the worker's environment lacks a
        ``BENCH_ENDPOINT_*`` variable the apply needs.
    :raise VllmApiKeyError: when the Secret's api-key field is absent, blank, or not
        base64-encoded UTF-8.
    """
    return open_bench_endpoint(
        terraform=build_terraform(),
        s3_client=s3_client,
        stack_dir=read_stack_dir(os.environ),
        topology=read_topology(os.environ),
        api_key=read_vllm_api_key(kubectl),
        results_bucket=bucket,
    )
