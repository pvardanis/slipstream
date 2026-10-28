"""Tests that `just obs-pivot`'s demo span stays out of vLLM's request counts.

`stack-up` runs `obs-pivot`, which posts one demo span through the Collector to Grafana
Cloud on every bring-up. Span metrics count every span they see, split by the sending
service's service.name, so a demo span sent as vLLM would add one request to vLLM's
counts per bring-up.
"""

import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_OBS_JUST = _REPO / "just" / "obs.just"


def _get_demo_span_resource() -> dict[str, str]:
    recipe = _OBS_JUST.read_text(encoding="utf-8")
    (body,) = re.findall(r"-d '(\{\"resourceSpans\".*?)';", recipe)
    payload = json.loads(body.replace("'\"${model}\"'", "model"))
    (resource_spans,) = payload["resourceSpans"]
    return {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in resource_spans["resource"]["attributes"]
    }


def test_the_demo_span_is_not_sent_as_vllm() -> None:
    assert _get_demo_span_resource()["service.name"] == "obs-pivot"
