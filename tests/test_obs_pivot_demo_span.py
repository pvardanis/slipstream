"""Tests that `just obs-pivot`'s demo span stays out of vLLM's request counts.

`stack-up` runs `obs-pivot`, which posts one demo span through the Collector to Grafana
Cloud on every bring-up. Span metrics count every span they see, split by the sending
service's service.name, a dimension the span_metrics connector always keeps. A demo span
sent under the GPU replica's service name (OTEL_SERVICE_NAME in k8s/vllm-gpu.yaml) would
count once per bring-up in every vLLM request rate selected by service_name.
"""

import json
import subprocess
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_OTEL_CHECK = _REPO / "scripts" / "otel-collector-check.sh"
_VLLM_GPU_MANIFEST = _REPO / "k8s" / "vllm-gpu.yaml"


def _get_demo_span_resource() -> dict[str, str]:
    """The resource attributes of the demo trace `obs-pivot` sends."""
    body = subprocess.run(
        ["bash", str(_OTEL_CHECK), "demo-span", "model"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    payload = json.loads(body)
    (resource_spans,) = payload["resourceSpans"]
    return {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in resource_spans["resource"]["attributes"]
    }


def _get_vllm_service_name() -> str:
    """The service.name the GPU replica's spans carry."""
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
