"""Tests that the Collector's Deployment reads what the eks stack publishes.

k8s/otel-collector.yaml takes its Grafana Cloud endpoints from the grafana-cloud-endpoints
ConfigMap (terraform/eks/otel_collector.tf) and its write token from the grafana-cloud-write
Secret (terraform/eks/external_secrets.tf). Kubernetes only checks a reference when the pod
starts, so a renamed key on either side would pass every plan and apply and leave the
Collector in CreateContainerConfigError. These tests read both sides from the repo.
"""

import re
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[1]
_MANIFEST = _REPO / "k8s" / "otel-collector.yaml"
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
