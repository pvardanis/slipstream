"""Tests that the GPU NodePool caps GPU spend at the serving fleet's ceiling and never
moves a running replica.

The GPU NodePool (k8s/gpu-node-pool.yaml, applied by `just gpu-pool-up`) is the hard cap
on GPU nodes: two GPUs, on-demand g5.xlarge only, the account's whole on-demand G quota
of 8 vCPU (ADR-0031). Each serving fleet replica holds one GPU, so the cap equals the
fleet's maxReplicaCount (k8s/serving/scaled-object.yaml): a lower cap leaves a replica
Pending, a higher one lets a mistake launch a node past the quota. Only empty nodes are
consolidated, so the node a 2→1 scale-down empties is reaped rather than handed the
surviving replica.
"""

from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[1]
_NODE_POOLS = _REPO / "k8s" / "gpu-node-pool.yaml"
_SCALED_OBJECT = _REPO / "k8s" / "serving" / "scaled-object.yaml"


def _get_node_pool() -> dict[str, Any]:
    documents = yaml.safe_load_all(_NODE_POOLS.read_text(encoding="utf-8"))
    (node_pool,) = [d for d in documents if d and d["kind"] == "NodePool"]
    return node_pool


def test_node_pool_limits_itself_to_two_gpus() -> None:
    assert _get_node_pool()["spec"]["limits"] == {"nvidia.com/gpu": "2"}


def test_node_pool_launches_on_demand_g5_xlarge_only() -> None:
    requirements = _get_node_pool()["spec"]["template"]["spec"]["requirements"]

    assert sorted(requirements, key=lambda r: r["key"]) == [
        {
            "key": "karpenter.sh/capacity-type",
            "operator": "In",
            "values": ["on-demand"],
        },
        {
            "key": "node.kubernetes.io/instance-type",
            "operator": "In",
            "values": ["g5.xlarge"],
        },
    ]


def test_node_pool_gpu_limit_equals_serving_fleet_ceiling() -> None:
    gpu_limit = int(_get_node_pool()["spec"]["limits"]["nvidia.com/gpu"])
    scaled_object = yaml.safe_load(_SCALED_OBJECT.read_text(encoding="utf-8"))

    assert gpu_limit == scaled_object["spec"]["maxReplicaCount"]


def test_node_pool_consolidates_only_empty_nodes() -> None:
    assert _get_node_pool()["spec"]["disruption"] == {
        "consolidationPolicy": "WhenEmpty",
        "consolidateAfter": "1m",
    }
