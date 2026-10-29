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
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[1]
_OTEL_CHECK = _REPO / "scripts" / "otel-collector-check.sh"
_VLLM_GPU_MANIFEST = _REPO / "k8s" / "vllm-gpu.yaml"
_OBS_JUST = _REPO / "just" / "obs.just"


def _get_demo_trace() -> dict[str, Any]:
    """The demo trace `obs-pivot` sends."""
    result = subprocess.run(
        ["bash", str(_OTEL_CHECK), "demo-span", "model"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (resource_spans,) = json.loads(result.stdout)["resourceSpans"]
    return resource_spans


def _get_string_attributes(attributes: list[dict[str, Any]]) -> dict[str, str]:
    """The string-valued attributes of an OTLP attribute list, by key."""
    return {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in attributes
        if "stringValue" in attribute["value"]
    }


def _get_demo_span_resource() -> dict[str, str]:
    """The resource attributes of the demo trace `obs-pivot` sends."""
    return _get_string_attributes(_get_demo_trace()["resource"]["attributes"])


def _get_demo_span_request_id() -> str:
    """The request_id the demo trace's span carries."""
    (scope,) = _get_demo_trace()["scopeSpans"]
    (span,) = scope["spans"]
    return _get_string_attributes(span["attributes"])["request_id"]


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


def test_obs_pivot_posts_the_demo_trace_and_finds_its_request_id() -> None:
    # The span metrics and the time-ranged trace search see what obs-pivot posts, and
    # obs-pivot passes only once the Collector's log shows the span's request_id.
    recipe = _OBS_JUST.read_text(encoding="utf-8")

    assert 'payload="$({{ otel_check_tool }} demo-span "${model}")"\n' in recipe
    assert "-H 'Content-Type: application/json' -d \"${payload}\"; then\n" in recipe
    assert "resourceSpans" not in recipe
    assert f"grep -A12 '{_get_demo_span_request_id()}'" in recipe
