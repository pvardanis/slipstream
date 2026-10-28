"""Tests that the Collector's Deployment reads what the eks stack publishes.

k8s/otel-collector.yaml takes its Grafana Cloud endpoints from the grafana-cloud-endpoints
ConfigMap (terraform/eks/otel_collector.tf) and its write token from the grafana-cloud-write
Secret (terraform/eks/external_secrets.tf). Kubernetes only checks a reference when the pod
starts, so a renamed key on either side would pass every plan and apply and leave the
Collector in CreateContainerConfigError. A variable wired to the wrong key, or one the
Collector config names but the Deployment never sets, starts a Collector that exports with
the wrong or an empty credential. These tests read every side from the repo.
"""

import re
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[1]
_MANIFEST = _REPO / "k8s" / "otel-collector.yaml"
_COLLECTOR_CONFIG = _REPO / "k8s" / "otel-collector-config.yaml"
_CONFIG_MAP_TF = _REPO / "terraform" / "eks" / "otel_collector.tf"
_EXTERNAL_SECRETS_TF = _REPO / "terraform" / "eks" / "external_secrets.tf"


def _get_collector_env() -> list[dict[str, Any]]:
    documents = yaml.safe_load_all(_MANIFEST.read_text(encoding="utf-8"))
    deployment = next(d for d in documents if d and d["kind"] == "Deployment")
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    return container["env"]


def _get_refs(kind: str, name: str) -> set[str]:
    return {
        var["valueFrom"][kind]["key"]
        for var in _get_collector_env()
        if kind in var.get("valueFrom", {}) and var["valueFrom"][kind]["name"] == name
    }


def _get_config_map_tf() -> tuple[str, set[str]]:
    resource = re.search(
        r'resource "kubernetes_config_map_v1" "grafana_cloud_endpoints" \{(.*?)\n\}',
        _CONFIG_MAP_TF.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert resource, f"no grafana_cloud_endpoints ConfigMap in {_CONFIG_MAP_TF}"
    name = re.search(r'name\s*=\s*"([^"]+)"', resource[1])
    data = re.search(r"data = \{(.*?)\n  \}", resource[1], re.DOTALL)
    assert name and data, f"the ConfigMap in {_CONFIG_MAP_TF} has no name or data"
    return name[1], set(re.findall(r"^\s*([\w-]+)\s*=", data[1], re.MULTILINE))


def test_the_collector_reads_every_key_of_the_endpoints_config_map() -> None:
    name, keys = _get_config_map_tf()

    assert _get_refs("configMapKeyRef", name) == keys


def test_the_collector_reads_the_write_token_secret_eso_syncs() -> None:
    secret = re.search(
        r'grafana_cloud_write_token_secret = \{ name = "([^"]+)", key = "([^"]+)" \}',
        _EXTERNAL_SECRETS_TF.read_text(encoding="utf-8"),
    )
    assert secret, (
        f"no grafana_cloud_write_token_secret local in {_EXTERNAL_SECRETS_TF}"
    )

    assert _get_refs("secretKeyRef", secret[1]) == {secret[2]}


def test_each_grafana_cloud_variable_reads_its_own_key() -> None:
    sources = {
        var["name"]: (ref["name"], ref["key"])
        for var in _get_collector_env()
        for ref in var.get("valueFrom", {}).values()
        if var["name"].startswith("GRAFANA_CLOUD_")
    }

    assert sources == {
        "GRAFANA_CLOUD_TRACES_ENDPOINT": ("grafana-cloud-endpoints", "traces-endpoint"),
        "GRAFANA_CLOUD_TRACES_USERNAME": ("grafana-cloud-endpoints", "traces-username"),
        "GRAFANA_CLOUD_METRICS_URL": ("grafana-cloud-endpoints", "metrics-url"),
        "GRAFANA_CLOUD_METRICS_USERNAME": (
            "grafana-cloud-endpoints",
            "metrics-username",
        ),
        "GRAFANA_CLOUD_WRITE_TOKEN": ("grafana-cloud-write", "token"),
    }


def test_the_config_reads_only_variables_the_deployment_sets() -> None:
    config = _COLLECTOR_CONFIG.read_text(encoding="utf-8")

    assert set(re.findall(r"\$\{env:(\w+)\}", config)) == {
        var["name"] for var in _get_collector_env()
    }


def test_each_store_authenticates_as_its_own_instance() -> None:
    config = yaml.safe_load(_COLLECTOR_CONFIG.read_text(encoding="utf-8"))
    exporters = config["exporters"]

    def get_credentials(exporter: str) -> dict[str, str]:
        authenticator = exporters[exporter]["auth"]["authenticator"]
        return config["extensions"][authenticator]["client_auth"]

    traces = get_credentials("otlp_grpc/grafana-cloud")
    metrics = get_credentials("prometheus_remote_write/grafana-cloud")
    assert exporters["otlp_grpc/grafana-cloud"]["endpoint"] == (
        "${env:GRAFANA_CLOUD_TRACES_ENDPOINT}"
    )
    assert traces["username"] == "${env:GRAFANA_CLOUD_TRACES_USERNAME}"
    assert exporters["prometheus_remote_write/grafana-cloud"]["endpoint"] == (
        "${env:GRAFANA_CLOUD_METRICS_URL}"
    )
    assert metrics["username"] == "${env:GRAFANA_CLOUD_METRICS_USERNAME}"
    assert (
        traces["password"] == metrics["password"] == "${env:GRAFANA_CLOUD_WRITE_TOKEN}"
    )


def test_the_service_serves_the_collectors_own_metrics_port() -> None:
    documents = [
        d for d in yaml.safe_load_all(_MANIFEST.read_text(encoding="utf-8")) if d
    ]
    deployment = next(d for d in documents if d["kind"] == "Deployment")
    service = next(d for d in documents if d["kind"] == "Service")
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    config = yaml.safe_load(_COLLECTOR_CONFIG.read_text(encoding="utf-8"))
    (reader,) = config["service"]["telemetry"]["metrics"]["readers"]
    prometheus = reader["pull"]["exporter"]["prometheus"]

    assert prometheus["host"] == "0.0.0.0"
    assert {"name": "metrics", "containerPort": prometheus["port"]} in container[
        "ports"
    ]
    assert {
        "name": "metrics",
        "port": prometheus["port"],
        "targetPort": "metrics",
    } in service["spec"]["ports"]


def test_the_service_serves_the_collectors_otlp_grpc_receiver() -> None:
    # vLLM sends its spans to the Service's otlp-grpc port; the receiver must listen
    # on every interface at the port the Service forwards to.
    documents = [
        d for d in yaml.safe_load_all(_MANIFEST.read_text(encoding="utf-8")) if d
    ]
    deployment = next(d for d in documents if d["kind"] == "Deployment")
    service = next(d for d in documents if d["kind"] == "Service")
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    config = yaml.safe_load(_COLLECTOR_CONFIG.read_text(encoding="utf-8"))
    host, port = config["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"].split(":")

    assert host == "0.0.0.0"
    assert {"name": "otlp-grpc", "containerPort": int(port)} in container["ports"]
    assert {
        "name": "otlp-grpc",
        "port": int(port),
        "targetPort": "otlp-grpc",
    } in service["spec"]["ports"]
