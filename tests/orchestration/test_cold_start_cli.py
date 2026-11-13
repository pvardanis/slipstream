"""The ``register-cold-start`` command: register the cold-start breakdown as a deployment.

Driven with the flow's ``deploy`` faked: the deployment is registered as a module-path
entrypoint on the process pool with no image build, its region and bucket as default
parameters, so a trigger supplies only the run id. The run sends no requests, so no
bench-client image is a parameter.
"""

import inspect
from typing import Any

import pytest
from prefect.deployments.runner import EntrypointType
from typer.testing import CliRunner

from slipstream_bench.orchestration import cli as cli_module
from slipstream_bench.orchestration.cli import app

_CLUSTER_INPUTS = {"region": "us-east-1", "bucket": "bench-bucket"}


def _invoke(monkeypatch: pytest.MonkeyPatch, **options: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_deploy(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "dep-cs"

    monkeypatch.setattr(cli_module.cold_start_flow, "deploy", _fake_deploy)
    monkeypatch.setattr(cli_module, "_require_work_pool", lambda _wp: None)
    argv = ["register-cold-start"]
    for name, value in {**_CLUSTER_INPUTS, **options}.items():
        argv += [f"--{name}", value]
    result = CliRunner().invoke(app, argv)
    captured["exit_code"] = result.exit_code
    captured["output"] = result.output
    return captured


def test_register_cold_start_registers_a_module_path_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _invoke(monkeypatch)

    assert captured["exit_code"] == 0, captured["output"]
    assert "dep-cs" in captured["output"]
    assert captured["name"] == "cold-start-breakdown"
    assert captured["work_pool_name"] == "sweep-pool"
    assert captured["entrypoint_type"] is EntrypointType.MODULE_PATH
    assert (captured["build"], captured["push"]) == (False, False)
    assert captured["parameters"] == {"region": "us-east-1", "bucket": "bench-bucket"}
    # run_id is the one flow parameter left for the trigger to supply.
    flow_parameters = inspect.signature(cli_module.cold_start_flow.fn).parameters
    assert set(flow_parameters) - set(captured["parameters"]) == {"run_id"}


@pytest.mark.parametrize("option", ["region", "bucket", "work-pool"])
def test_register_cold_start_rejects_an_empty_cluster_input(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    captured = _invoke(monkeypatch, **{option: "  "})

    assert captured["exit_code"] == 2
    assert f"--{option}" in captured["output"]
    assert "parameters" not in captured
