"""Set up a parent flow run: surface the core's milestone logs and tag the run's group.

The knob sweep and the KV cache offload A/B call both at their composition roots, so the
Prefect-free core's INFO milestones reach the UI and the run joins the ``run=<run-id>``
filter.
"""

import logging

from prefect.client.orchestration import get_client
from prefect.runtime import flow_run

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
