"""Tests for the serving fleet's manifest, rendered from the bench Engine's template.

The serving fleet's Deployment and Service are generated from ``k8s/vllm-gpu.yaml`` at
the 0%-share knob-sweep winner (``32 fp8 off``) under their own name, labels and
``serving`` namespace, with no ``replicas`` so Argo CD never resets KEDA's count, and a
ClusterIP Service, so traffic reaches the fleet only through its proxy (ADR-0031). The
committed render must equal a fresh one, so the two pod specs keep one source.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from slipstream_bench.orchestration.cli import app
from slipstream_bench.orchestration.cluster import render_gpu_manifest
from slipstream_bench.orchestration.serving_manifest import (
    SERVING_ENGINE_ARGS,
    render_serving_manifest,
)

_REPO = Path(__file__).parents[2]
_TEMPLATE = _REPO / "k8s" / "vllm-gpu.yaml"
_COMMITTED = _REPO / "k8s" / "serving" / "vllm-serving.yaml"
_PROXY_MANIFEST = _REPO / "k8s" / "serving" / "serving-proxy.yaml"
_CPU_MANIFEST = _REPO / "k8s" / "vllm.yaml"
_BENCH_ENDPOINT_VARIABLES = _REPO / "terraform" / "bench-endpoint" / "variables.tf"


def _template_text() -> str:
    """Return the bench Engine's committed template."""
    return _TEMPLATE.read_text(encoding="utf-8")


def _render_serving() -> str:
    """Render the serving fleet's manifest from the committed template."""
    return render_serving_manifest(_template_text())


def _documents_by_kind(manifest_text: str) -> dict[str, dict[str, Any]]:
    """Key a multi-document manifest's documents by kind."""
    return {doc["kind"]: doc for doc in yaml.safe_load_all(manifest_text)}


def _render_documents() -> dict[str, dict[str, Any]]:
    """Render the serving fleet's manifest and key its documents by kind."""
    return _documents_by_kind(_render_serving())


def _render_bench_documents() -> dict[str, dict[str, Any]]:
    """Render the template as the bench Engine at the serving knobs, keyed by kind."""
    return _documents_by_kind(
        render_gpu_manifest(
            _template_text(), engine_args=SERVING_ENGINE_ARGS, kv_cache_offload=False
        )
    )


def _arg_value(args: list[str], flag: str) -> str:
    """Return the value following ``flag`` in a container's args."""
    return args[args.index(flag) + 1]


def test_the_serving_engine_runs_the_zero_share_winner() -> None:
    """32 fp8 off: max-num-seqs 32, fp8 KV cache, prefix caching off, no cap, no offload."""
    deployment = _render_documents()["Deployment"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]

    assert _arg_value(args, "--max-num-seqs") == "32"
    assert _arg_value(args, "--kv-cache-dtype") == "fp8"
    assert "--no-enable-prefix-caching" in args
    assert "--enable-prefix-caching" not in args
    assert _arg_value(args, "--kv-cache-memory-bytes") == "None"
    assert _arg_value(args, "--kv-transfer-config") == "None"
    assert container["resources"]["limits"]["memory"] == "8Gi"


def test_the_serving_fleet_lives_in_the_serving_namespace() -> None:
    documents = _render_documents()

    assert documents["Deployment"]["metadata"]["namespace"] == "serving"
    assert documents["Service"]["metadata"]["namespace"] == "serving"


def test_the_serving_manifest_holds_only_the_deployment_and_service() -> None:
    """The workloads AppProject syncs no cluster-scoped resource, so no Namespace."""
    kinds = [doc["kind"] for doc in yaml.safe_load_all(_render_serving())]

    assert sorted(kinds) == ["Deployment", "Service"]


@pytest.mark.parametrize(
    "extra_document",
    [
        "kind: ConfigMap\nmetadata:\n  name: extra\n",
        "kind: Service\nmetadata:\n  name: vllm-gpu-metrics\n",
    ],
    ids=["another-kind", "a-second-service"],
)
def test_a_template_beyond_one_deployment_and_one_service_is_rejected(
    extra_document: str,
) -> None:
    """Only the Deployment and Service get the fleet's selectors; anything else would
    come out renamed but still pointing at the bench Engine."""
    template = f"{_template_text()}\n---\n{extra_document}"

    with pytest.raises(ValueError, match="one Deployment and one Service"):
        render_serving_manifest(template)


def test_the_serving_deployment_has_its_own_name_and_labels() -> None:
    deployment = _render_documents()["Deployment"]

    assert deployment["metadata"]["name"] == "vllm-serving"
    assert deployment["metadata"]["labels"] == {"app": "vllm-serving"}
    assert deployment["spec"]["selector"] == {"matchLabels": {"app": "vllm-serving"}}
    assert deployment["spec"]["template"]["metadata"]["labels"] == {
        "app": "vllm-serving"
    }


def test_the_serving_deployment_leaves_its_replica_count_to_the_autoscaler() -> None:
    """No replicas field, so an Argo CD sync never resets the count KEDA set."""
    deployment = _render_documents()["Deployment"]

    assert "replicas" not in deployment["spec"]


def test_the_serving_service_is_reached_only_inside_the_cluster() -> None:
    """ClusterIP, no NodePort: outside traffic goes through the proxy, which wakes it."""
    service = _render_documents()["Service"]

    assert service["metadata"]["name"] == "vllm-serving"
    assert service["metadata"]["labels"] == {"app": "vllm-serving"}
    assert service["spec"]["selector"] == {"app": "vllm-serving"}
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 8000, "targetPort": "http"}
    ]


def _service_node_port(manifest: Path) -> int:
    """Return the one nodePort a manifest's Services claim."""
    (node_port,) = [
        port["nodePort"]
        for doc in yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
        if doc["kind"] == "Service"
        for port in doc["spec"]["ports"]
        if "nodePort" in port
    ]
    return node_port


def _bench_endpoint_default_node_port() -> int:
    """Return the bench endpoint stack's default target NodePort, vllm_nodeport."""
    variables = _BENCH_ENDPOINT_VARIABLES.read_text(encoding="utf-8")
    match = re.search(
        r'variable "vllm_nodeport" \{[^}]*?default\s*=\s*(\d+)', variables
    )
    assert match is not None, "bench-endpoint has no vllm_nodeport default"
    return int(match[1])


def test_the_serving_proxy_node_port_is_apart_from_the_bench_ports() -> None:
    """The bench endpoint targets 30800 by default, which both bench replicas claim."""
    bench_ports = {
        _service_node_port(_TEMPLATE),
        _service_node_port(_CPU_MANIFEST),
        _bench_endpoint_default_node_port(),
    }

    assert _service_node_port(_PROXY_MANIFEST) not in bench_ports


def _drop_serving_identity(documents: dict[str, dict[str, Any]]) -> None:
    """Remove, in place, the spec fields the serving identity sets, leaving the rest."""
    deployment_spec = documents["Deployment"]["spec"]
    deployment_spec.pop("replicas", None)
    del deployment_spec["selector"]
    del deployment_spec["template"]["metadata"]["labels"]
    service_spec = documents["Service"]["spec"]
    del service_spec["selector"]
    del service_spec["type"]
    service_spec["ports"][0].pop("nodePort", None)


def test_the_serving_specs_are_the_bench_engine_specs() -> None:
    """Only identity, replicas and the Service type differ: the specs keep one source."""
    serving = _render_documents()
    bench = _render_bench_documents()

    _drop_serving_identity(serving)
    _drop_serving_identity(bench)

    for kind in ("Deployment", "Service"):
        assert serving[kind]["spec"] == bench[kind]["spec"], kind


def test_the_committed_serving_manifest_matches_a_fresh_render() -> None:
    """A template edit not re-rendered, or a hand edit, fails here."""
    assert _COMMITTED.read_text(encoding="utf-8") == _render_serving(), (
        f"{_COMMITTED.relative_to(_REPO)} is stale: run `just render-serving-manifest` "
        f"and commit the result"
    )


def test_the_serving_manifest_opens_with_a_generated_file_header() -> None:
    assert _render_serving().startswith("# Generated by `just render-serving-manifest`")


def test_the_render_command_writes_the_serving_manifest(tmp_path: Path) -> None:
    output = tmp_path / "serving" / "vllm-serving.yaml"

    result = CliRunner().invoke(
        app,
        [
            "render-serving-manifest",
            "--template",
            str(_TEMPLATE),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output.read_text(encoding="utf-8") == _render_serving()
