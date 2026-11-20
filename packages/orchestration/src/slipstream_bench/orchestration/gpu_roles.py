"""The two roles that share the GPU quota, and the check of whether one holds GPUs (ADR-0031).

The serving fleet and the bench Engine each run a Deployment of GPU replicas, and the
on-demand quota fits two GPUs in all, so a flow that needs the GPUs refuses to start while
the other role has replicas: the knob sweep, the KV cache A/B and the cold-start flow
inspect the serving fleet, and ADR-0031 has the autoscale flow inspect the bench Engine.
The check reads the role's Deployment over the injected
:data:`~slipstream_bench.orchestration.cluster.Kubectl` transport, under a get on that one
Deployment (``terraform/eks/worker.tf`` for the bench Engine,
``terraform/eks/worker_serving_read.tf`` for the serving fleet).
"""

from dataclasses import dataclass

from slipstream_bench.orchestration.cluster import (
    VLLM_DEPLOYMENT,
    VLLM_NAMESPACE,
    Kubectl,
)


@dataclass(frozen=True)
class GpuRole:
    """A role that runs GPU replicas as one Deployment.

    :param name: the role's name, as an error names it to the operator.
    :param namespace: the namespace the Deployment lives in.
    :param deployment: the Deployment's ``metadata.name``.
    """

    name: str
    namespace: str
    deployment: str


BENCH_ENGINE = GpuRole(
    name="bench engine", namespace=VLLM_NAMESPACE, deployment=VLLM_DEPLOYMENT
)
SERVING_FLEET = GpuRole(
    name="serving fleet", namespace="serving", deployment="vllm-serving"
)


class GpusHeldError(Exception):
    """A flow refused to start because the other role's Deployment has replicas."""


def role_has_replicas(kubectl: Kubectl, role: GpuRole) -> bool:
    """Report whether the role's Deployment asks for any replicas.

    A Deployment that does not exist holds no GPUs, so it reads as having none: the
    serving fleet is absent until Argo CD first syncs it.

    :param kubectl: the transport the Deployment is read through.
    :param role: the role whose Deployment is read.
    :return: whether the Deployment's ``spec.replicas`` is above zero.
    :raise ValueError: when the replica count is not an integer.
    :raise KubectlError: when the read fails, such as a get the worker is forbidden.
    """
    stdout = kubectl(
        [
            "-n",
            role.namespace,
            "get",
            "deployment",
            role.deployment,
            "--ignore-not-found",
            "-o",
            "jsonpath={.spec.replicas}",
        ],
        None,
    ).strip()

    if not stdout:
        return False

    try:
        return int(stdout) > 0
    except ValueError as error:
        raise ValueError(
            f"deployment/{role.deployment} in {role.namespace} reported replicas "
            f"{stdout!r}, not an integer: cannot tell whether the {role.name} holds "
            f"GPUs. Check `kubectl -n {role.namespace} get deployment {role.deployment}`."
        ) from error


def refuse_while_role_holds_gpus(kubectl: Kubectl, role: GpuRole) -> None:
    """Raise while the role's Deployment has replicas, so a run never starts short of GPUs.

    :param kubectl: the transport the Deployment is read through.
    :param role: the role that must hold no GPUs for the run to start.
    :raise GpusHeldError: when the role's Deployment has replicas.
    :raise KubectlError: when the read fails, such as a get the worker is forbidden.
    """
    if role_has_replicas(kubectl, role):
        raise GpusHeldError(
            f"the {role.name} holds GPUs: deployment/{role.deployment} in "
            f"{role.namespace} has replicas, and the two roles share the GPU quota. "
            f"Re-run once it is back at zero."
        )
