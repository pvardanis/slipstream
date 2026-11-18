"""The bench flows refuse to start while the serving fleet holds GPUs (ADR-0031).

The serving fleet and the bench Engine share the two-GPU quota, so the knob sweep, the
KV cache A/B and the cold-start flow each check the serving fleet's Deployment before any
GPU work and fail naming it. These tests run each real flow against an ephemeral Prefect
server over the committed config files, with only the ``Kubectl`` transport faked by a
cluster whose serving fleet has replicas and which fails any other call, so a flow that
froze rollouts, opened its bench endpoint or touched the bench Engine first would fail on
that call rather than on the refusal.
"""

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from prefect.testing.utilities import prefect_test_harness

import slipstream_bench.orchestration.flows.cold_start as cold_start_module
import slipstream_bench.orchestration.flows.knob_sweep as knob_sweep_module
import slipstream_bench.orchestration.flows.kv_ab as kv_ab_module
from slipstream_bench.orchestration.gpu_roles import GpusHeldError

_REPO = Path(__file__).parents[2]

_SERVING_FLEET_READ = [
    "-n",
    "serving",
    "get",
    "deployment",
    "vllm-serving",
    "--ignore-not-found",
    "-o",
    "jsonpath={.spec.replicas}",
]


class _ServingFleetUp:
    """A cluster whose serving fleet has two replicas; any other call fails the test."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append(list(args))
        if list(args) != _SERVING_FLEET_READ:
            raise AssertionError(f"kubectl call before the refusal: {list(args)}")
        return "2"


@pytest.fixture(autouse=True)
def _harness() -> Iterator[None]:
    with prefect_test_harness():
        yield


def _run_knob_sweep() -> Any:
    return knob_sweep_module.knob_sweep_flow(
        run_id="run1",
        region="us-east-1",
        bucket="bench",
        image_ref="bench:sha",
        model_yaml=_REPO / "model.yaml",
        sweep_grid=_REPO / "bench" / "configs" / "sweep-grid.yaml",
        vllm_manifest=_REPO / "k8s" / "vllm-gpu.yaml",
    )


def _run_kv_ab() -> Any:
    return kv_ab_module.kv_ab_flow(
        run_id="ab1",
        region="us-east-1",
        bucket="bench",
        image_ref="bench:sha",
        model_yaml=_REPO / "model.yaml",
        kv_ab_config=_REPO / "bench" / "configs" / "kv-cache-extension-ab.yaml",
        vllm_manifest=_REPO / "k8s" / "vllm-gpu.yaml",
    )


def _run_cold_start() -> Any:
    return cold_start_module.cold_start_flow(
        run_id="cs1", region="us-east-1", bucket="bench"
    )


_FLOWS = [
    pytest.param(knob_sweep_module, _run_knob_sweep, id="knob-sweep"),
    pytest.param(kv_ab_module, _run_kv_ab, id="kv-ab"),
    pytest.param(cold_start_module, _run_cold_start, id="cold-start"),
]


@pytest.mark.parametrize(("module", "run_flow"), _FLOWS)
def test_the_flow_refuses_to_start_while_the_serving_fleet_has_replicas(
    monkeypatch: pytest.MonkeyPatch, module: Any, run_flow: Callable[[], Any]
) -> None:
    cluster = _ServingFleetUp()
    monkeypatch.setattr(module, "build_kubectl", lambda: cluster)

    with pytest.raises(GpusHeldError, match="the serving fleet holds GPUs"):
        run_flow()

    assert cluster.calls == [_SERVING_FLEET_READ]
