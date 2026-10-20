"""The ``register-kv-ab`` command: register the KV cache offload A/B as a Prefect deployment.

Driven with the flow's ``deploy`` faked: the deployment is registered as a module-path
entrypoint on the process pool with no image build, its cluster-stable inputs and in-image
paths as default parameters, so a trigger supplies only the run id, the bench host, and
optionally the workload config.
"""

from typing import Any

import pytest
from prefect.deployments.runner import EntrypointType
from typer.testing import CliRunner

from slipstream_bench.orchestration import cli as cli_module
from slipstream_bench.orchestration.cli import app

_CLUSTER_INPUTS = {
    "region": "us-east-1",
    "bucket": "bench-bucket",
    "image-ref": "repo:tag",
}


def _invoke(monkeypatch: pytest.MonkeyPatch, **options: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_deploy(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "dep-kv"

    monkeypatch.setattr(cli_module.kv_ab_flow, "deploy", _fake_deploy)
    monkeypatch.setattr(cli_module, "_require_work_pool", lambda _wp: None)
    argv = ["register-kv-ab"]
    for name, value in {**_CLUSTER_INPUTS, **options}.items():
        argv += [f"--{name}", value]
    result = CliRunner().invoke(app, argv)
    captured["exit_code"] = result.exit_code
    captured["output"] = result.output
    return captured


def test_register_kv_ab_registers_a_module_path_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _invoke(monkeypatch)

    assert captured["exit_code"] == 0, captured["output"]
    assert "dep-kv" in captured["output"]
    assert captured["name"] == "kv-cache-extension-ab"
    assert captured["work_pool_name"] == "sweep-pool"
    assert captured["entrypoint_type"] is EntrypointType.MODULE_PATH
    assert (captured["build"], captured["push"]) == (False, False)
    # Keyed exactly as the flow's parameters: a mis-named default fails on the worker.
    assert captured["parameters"] == {
        "region": "us-east-1",
        "bucket": "bench-bucket",
        "image_ref": "repo:tag",
        "model_yaml": "/app/model.yaml",
        "kv_ab_config": "/app/bench/kv-cache-extension-ab.yaml",
        "vllm_manifest": "/app/k8s/vllm-gpu.yaml",
        "metrics_url": "http://vllm-gpu.slipstream.svc:8000/metrics",
        "retries": 0,
    }


def test_register_kv_ab_threads_overrides_onto_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _invoke(
        monkeypatch,
        **{
            "kv-ab-config": "/app/bench/kv-cache-extension-ab.chat.yaml",
            "retries": "2",
        },
    )

    assert captured["exit_code"] == 0, captured["output"]
    parameters = captured["parameters"]
    assert parameters["kv_ab_config"] == "/app/bench/kv-cache-extension-ab.chat.yaml"
    assert parameters["retries"] == 2


@pytest.mark.parametrize("option", ["region", "bucket", "image-ref"])
def test_register_kv_ab_rejects_an_empty_cluster_input(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    captured = _invoke(monkeypatch, **{option: "  "})

    assert captured["exit_code"] == 2
    assert "parameters" not in captured
