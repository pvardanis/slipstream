"""Read one cold start's cluster-side phases through the ``Kubectl`` transport (ADR-0030).

Once the replica's pod is Ready, the cold-start-breakdown flow reads the pod, the
namespace's events and the NodeClaim hosting the pod, and breaks the pod-created → Ready
total into capacity wait (pod created → the NodeClaim's ``Launched``), node launch
(``Launched`` → ``Initialized``) and image pull (kubelet's ``Pulling`` → ``Pulled`` for
the engine container); the rest is unattributed.

Kubernetes records these markers to the second. Every event the cluster writes has a
null ``eventTime``, so an event is timed by its ``firstTimestamp``; repeated events are
aggregated into one record with a ``count``. The namespace's events also hold other
objects' events, so they are filtered by the pod's kind and name. Karpenter's NodeClaim
events are cluster-scoped, so the NodeClaim's times come from its conditions. A missing
marker raises :class:`ColdStartMarkerError` naming it, so a run never reports a wrong
phase.
"""

import json
from datetime import datetime
from typing import Any

from slipstream_bench.contract.cold_start_record import ColdStartBreakdown
from slipstream_bench.orchestration.cluster import VLLM_NAMESPACE, Kubectl

# The replica's pods carry the Deployment's selector label (k8s/vllm-gpu.yaml).
_REPLICA_SELECTOR = "app=vllm-gpu"
# The engine container whose image pull is the image-pull phase.
_ENGINE_CONTAINER = "vllm"


class ColdStartMarkerError(Exception):
    """A cold start whose cluster objects lack a marker a phase is bounded by."""


def read_cold_start_breakdown(
    kubectl: Kubectl,
    *,
    run: int,
    namespace: str = VLLM_NAMESPACE,
    selector: str = _REPLICA_SELECTOR,
    container: str = _ENGINE_CONTAINER,
) -> ColdStartBreakdown:
    """Read the Ready replica's pod, events and NodeClaim and break its cold start down.

    :param kubectl: the transport the reads are issued through.
    :param run: the run's 1-based number within its measurement.
    :param namespace: the namespace the replica runs in.
    :param selector: the label selector matching the replica's pod.
    :param container: the engine container whose image pull is timed.
    :return: the run's total and its cluster-side phases.
    :raise ColdStartMarkerError: when a marker a phase is bounded by is missing.
    """
    pod = _get_replica_pod(kubectl, namespace=namespace, selector=selector)
    created = _parse_time(pod["metadata"]["creationTimestamp"])
    ready = _get_condition_time(pod, "Ready", kind="pod")
    nodeclaim = _get_hosting_nodeclaim(kubectl, pod)
    launched = _get_condition_time(nodeclaim, "Launched", kind="NodeClaim")
    initialized = _get_condition_time(nodeclaim, "Initialized", kind="NodeClaim")
    events = _list_container_events(kubectl, pod, container=container)
    pulling = _get_event_time(events, "Pulling", pod=pod, container=container)
    pulled = _get_event_time(events, "Pulled", pod=pod, container=container)
    return ColdStartBreakdown(
        run=run,
        total_s=_seconds(created, ready),
        capacity_wait_s=_seconds(created, launched),
        node_launch_s=_seconds(launched, initialized),
        image_pull_s=_seconds(pulling, pulled),
    )


def _get_replica_pod(
    kubectl: Kubectl, *, namespace: str, selector: str
) -> dict[str, Any]:
    """Return the replica's one pod."""
    pods = _get_json(kubectl, ["-n", namespace, "get", "pods", "-l", selector])["items"]
    if len(pods) != 1:
        raise ColdStartMarkerError(
            f"{len(pods)} pods match {selector} in {namespace}: a cold start is read "
            f"from the replica's one pod once it is Ready"
        )
    return pods[0]


def _get_hosting_nodeclaim(kubectl: Kubectl, pod: dict[str, Any]) -> dict[str, Any]:
    """Return the NodeClaim whose node the pod was scheduled onto."""
    node = pod["spec"]["nodeName"]
    nodeclaims = _get_json(kubectl, ["get", "nodeclaims"])["items"]
    for claim in nodeclaims:
        if claim.get("status", {}).get("nodeName") == node:
            return claim
    raise ColdStartMarkerError(
        f"no NodeClaim hosts node {node} of pod {pod['metadata']['name']}: capacity "
        f"wait and node launch are bounded by its Launched and Initialized conditions"
    )


def _list_container_events(
    kubectl: Kubectl, pod: dict[str, Any], *, container: str
) -> list[dict[str, Any]]:
    """Return the namespace's events about the pod's engine container."""
    metadata = pod["metadata"]
    events = _get_json(kubectl, ["-n", metadata["namespace"], "get", "events"])
    field_path = f"spec.containers{{{container}}}"
    return [
        event
        for event in events["items"]
        if event["involvedObject"].get("kind") == "Pod"
        and event["involvedObject"].get("name") == metadata["name"]
        and event["involvedObject"].get("fieldPath") == field_path
    ]


def _get_condition_time(obj: dict[str, Any], condition: str, *, kind: str) -> datetime:
    """Return when the object's condition turned true."""
    for entry in obj.get("status", {}).get("conditions", []):
        if entry["type"] == condition and entry["status"] == "True":
            return _parse_time(entry["lastTransitionTime"])
    raise ColdStartMarkerError(
        f"{kind} {obj['metadata']['name']} has no true {condition} condition, a marker "
        f"the cold-start breakdown is bounded by"
    )


def _get_event_time(
    events: list[dict[str, Any]], reason: str, *, pod: dict[str, Any], container: str
) -> datetime:
    """Return when the first event with the reason was recorded."""
    times = [_parse_time(e["firstTimestamp"]) for e in events if e["reason"] == reason]
    if not times:
        raise ColdStartMarkerError(
            f"no {reason} event for container {container} of pod "
            f"{pod['metadata']['name']}: the image pull is bounded by it, and events "
            f"expire about an hour after they are recorded, so read them as soon as "
            f"the pod is Ready"
        )
    return min(times)


def _get_json(kubectl: Kubectl, args: list[str]) -> dict[str, Any]:
    """Issue a ``kubectl get`` with JSON output and parse it."""
    return json.loads(kubectl([*args, "-o", "json"], None))


def _parse_time(stamp: str) -> datetime:
    """Parse a Kubernetes RFC 3339 timestamp."""
    return datetime.fromisoformat(stamp)


def _seconds(start: datetime, end: datetime) -> float:
    """Return the seconds from start to end."""
    return (end - start).total_seconds()
