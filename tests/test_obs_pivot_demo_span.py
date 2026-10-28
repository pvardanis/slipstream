"""Tests that `just obs-pivot`'s demo span stays out of vLLM's request counts.

`stack-up` runs `obs-pivot`, which posts one demo span through the Collector to Grafana
Cloud on every bring-up. Span metrics count every span they see, split by the sending
service's service.name, a dimension the span_metrics connector always keeps. A demo span
sent as service vllm would count once per bring-up in every vLLM request rate selected
by service_name.
"""

import json
import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_OBS_JUST = _REPO / "just" / "obs.just"
_VLLM_GPU_MANIFEST = _REPO / "k8s" / "vllm-gpu.yaml"


def _get_demo_span_resource() -> dict[str, str]:
    recipe = _OBS_JUST.read_text(encoding="utf-8")
    bodies = re.findall(r"-d '(\{\"resourceSpans\".*?)';", recipe)
    assert len(bodies) == 1, f"expected one OTLP span payload in {_OBS_JUST}: {bodies}"
    (body,) = bodies
    payload = json.loads(body.replace("'\"${model}\"'", "model"))
    (resource_spans,) = payload["resourceSpans"]
    return {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in resource_spans["resource"]["attributes"]
    }


def _get_vllm_service_name() -> str:
    (deployment,) = (
        doc
        for doc in yaml.safe_load_all(_VLLM_GPU_MANIFEST.read_text(encoding="utf-8"))
        if doc and doc["kind"] == "Deployment"
    )
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    env = {var["name"]: var for var in container["env"]}
    return env["OTEL_SERVICE_NAME"]["value"]


def test_the_demo_span_is_not_sent_as_vllm() -> None:
    assert _get_demo_span_resource()["service.name"] != _get_vllm_service_name()
