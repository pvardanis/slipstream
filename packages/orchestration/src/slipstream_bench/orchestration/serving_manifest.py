"""Render the serving fleet's manifest from the bench Engine's template (ADR-0031).

The serving fleet runs the same pod spec as the bench Engine (``k8s/vllm-gpu.yaml``), so
its manifest is generated from that template rather than copied by hand. The render fixes
the engine knobs at the 0%-share knob-sweep winner, ``32 fp8 off``, through the same
:func:`render_gpu_manifest` the flows use, then gives the fleet its own name, labels,
``serving`` namespace and NodePort, and drops ``replicas`` so an Argo CD sync never
resets the count KEDA alone owns. Neither the template nor its renderers change.
"""

from typing import Any

import yaml

from slipstream_bench.orchestration.cluster import render_gpu_manifest
from slipstream_bench.orchestration.grid import EngineArgs, get_kv_engine_token

# The 0%-share knob-sweep winner, `32 fp8 off` (ADR-0009, ADR-0031): the share with the
# lowest concurrency ceiling, so the autoscaler's threshold is set against the most
# conservative config. The KV pool stays uncapped, as the sweep measured it.
SERVING_ENGINE_ARGS = EngineArgs(
    max_num_seqs=32,
    kv_engine_token=get_kv_engine_token("fp8"),
    prefix_caching_flag="--no-enable-prefix-caching",
    kv_cache_memory_bytes=None,
)

SERVING_NAMESPACE = "serving"
SERVING_NAME = "vllm-serving"
_SERVING_LABELS = {"app": SERVING_NAME}

# The bench Engine's Service claims NodePort 30800, the bench endpoint's default target
# (its vllm_nodeport input), so the serving fleet's Service takes the next port: both
# Services coexist on the node group, and a run points the bench endpoint here by
# overriding that input.
SERVING_NODE_PORT = 30801


def render_serving_manifest(template_text: str) -> str:
    """Render the serving fleet's manifest from the bench Engine's template.

    :param template_text: the raw ``k8s/vllm-gpu.yaml`` text with its placeholders.
    :return: the serving fleet's manifest.
    :raise ValueError: when the template's Service already claims the serving fleet's
        NodePort — the cluster would reject whichever Service applies second.
    """
    rendered = render_gpu_manifest(
        template_text, engine_args=SERVING_ENGINE_ARGS, kv_cache_offload=False
    )
    documents = [_to_serving(doc) for doc in yaml.safe_load_all(rendered)]
    return yaml.safe_dump_all(documents, sort_keys=False)


def _to_serving(document: dict[str, Any]) -> dict[str, Any]:
    """Give one of the template's documents the serving fleet's identity."""
    metadata = document["metadata"]
    if document["kind"] == "Namespace":
        metadata["name"] = SERVING_NAMESPACE
        return document
    metadata.update(name=SERVING_NAME, namespace=SERVING_NAMESPACE)
    metadata["labels"] = dict(_SERVING_LABELS)
    spec = document["spec"]
    if document["kind"] == "Deployment":
        del spec["replicas"]
        spec["selector"] = {"matchLabels": dict(_SERVING_LABELS)}
        spec["template"]["metadata"]["labels"] = dict(_SERVING_LABELS)
    elif document["kind"] == "Service":
        spec["selector"] = dict(_SERVING_LABELS)
        (port,) = spec["ports"]
        if port["nodePort"] == SERVING_NODE_PORT:
            raise ValueError(
                f"the bench Engine's Service already claims NodePort "
                f"{SERVING_NODE_PORT}, the serving fleet's: move one of them so the "
                f"two Services do not collide"
            )
        port["nodePort"] = SERVING_NODE_PORT
    return document
