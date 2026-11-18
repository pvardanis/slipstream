"""The GPU role check: whether the serving fleet or the bench Engine holds GPUs (ADR-0031).

Exercises role_has_replicas and refuse_while_role_holds_gpus against a fake kubectl that
records the argv issued and serves the Deployment's replica count, or nothing when the
Deployment does not exist. Asserts the check reads the role's own Deployment in its own
namespace, that zero or no Deployment holds no GPUs, and that the refusal names the role.
"""

from collections.abc import Sequence

import pytest

from slipstream_bench.orchestration.gpu_roles import (
    BENCH_ENGINE,
    SERVING_FLEET,
    GpusHeldError,
    refuse_while_role_holds_gpus,
    role_has_replicas,
)


class _FakeKubectl:
    """Record each argv; serve ``replicas`` as the Deployment's ``.spec.replicas``."""

    def __init__(self, replicas: str) -> None:
        self.replicas = replicas
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append(list(args))
        return self.replicas


def test_the_check_reads_the_serving_fleet_s_deployment_in_the_serving_namespace() -> (
    None
):
    kubectl = _FakeKubectl("0")

    role_has_replicas(kubectl, SERVING_FLEET)

    assert kubectl.calls == [
        [
            "-n",
            "serving",
            "get",
            "deployment",
            "vllm-serving",
            "--ignore-not-found",
            "-o",
            "jsonpath={.spec.replicas}",
        ]
    ]


def test_the_check_reads_the_bench_engine_s_deployment_in_the_slipstream_namespace() -> (
    None
):
    kubectl = _FakeKubectl("0")

    role_has_replicas(kubectl, BENCH_ENGINE)

    assert kubectl.calls[0][:5] == ["-n", "slipstream", "get", "deployment", "vllm-gpu"]


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("1", True), ("2", True), ("0", False), ("", False)],
    ids=["one", "two", "zero", "no-deployment"],
)
def test_the_check_reports_whether_the_role_has_replicas(
    stdout: str, expected: bool
) -> None:
    # A Deployment that does not exist, such as a serving fleet not yet synced, reads as
    # empty stdout under --ignore-not-found and holds no GPUs.
    assert role_has_replicas(_FakeKubectl(stdout), SERVING_FLEET) is expected


def test_the_check_raises_on_a_replica_count_it_cannot_read() -> None:
    with pytest.raises(ValueError, match="deployment/vllm-serving in serving"):
        role_has_replicas(_FakeKubectl("two"), SERVING_FLEET)


def test_the_refusal_names_the_role_that_holds_the_gpus() -> None:
    with pytest.raises(GpusHeldError, match="the serving fleet") as excinfo:
        refuse_while_role_holds_gpus(_FakeKubectl("2"), SERVING_FLEET)

    assert "deployment/vllm-serving in serving" in str(excinfo.value)


def test_the_refusal_passes_while_the_role_has_no_replicas() -> None:
    refuse_while_role_holds_gpus(_FakeKubectl("0"), SERVING_FLEET)
