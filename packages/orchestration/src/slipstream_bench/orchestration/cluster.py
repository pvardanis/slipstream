"""The parent knob sweep's two in-cluster collaborators: GPU redeploy and ceiling scrape.

ADR-0015: the parent flow redeploys the GPU per engine point (Tier-1) then scrapes the
concurrency ceiling from vLLM's startup log before running the point's Tier-2 ladder.
Off-laptop, both run from the EKS worker over ``kubectl`` against the in-cluster API,
under a ServiceAccount whose RBAC (``terraform/eks/worker.tf``) grants only get/patch on
``deploy/vllm-gpu`` and reads of its rollout and pod logs — nothing wider. So the
redeploy applies **only** the Deployment document (a get + patch the RBAC allows), never
the manifest's Namespace or Service (which it has no permission to touch and which the
operator's ``just gpu-deploy`` created once at stack-up).

The ``kubectl`` transport is injected as a :data:`Kubectl` callable, so the redeploy and
scrape are exercised against a fake that records the argv and stdin issued — no cluster.
The scrape returns the ceiling text it read (the composition root records it) but raises
:class:`CeilingScrapeError` when the engine reported none, so a point's ladder never runs
against a garbage ceiling — the one failure an unattended sweep cannot tolerate.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence

import yaml

from slipstream_bench.contract import EnginePoint, SweepGrid
from slipstream_bench.orchestration.grid import EngineArgs, get_engine_args


class CeilingScrapeError(Exception):
    """No concurrency ceiling could be scraped for an engine point.

    The parent knob sweep scrapes each redeployed engine's reported ceiling before
    running the point's Tier-2 ladder (ADR-0015). A scrape that finds none raises this
    rather than returning empty, so the point fails loudly instead of measuring its
    ladder against a garbage ceiling — the one failure an unattended sweep cannot
    tolerate.
    """


# vLLM logs its VRAM/KV-budget concurrency estimate once at startup, e.g.
# "Maximum concurrency for 4,096 tokens per request: 10.30x". The scrape reads this
# predicted ceiling; the grid fixes the ladder, so it is recorded, not threaded (ADR-0009).
_CEILING_PATTERN = re.compile(
    r"Maximum concurrency for [\d,]+ tokens per request: [\d.]+x"
)

# The manifest placeholders the knob sweep renders per point (k8s/vllm-gpu.yaml). Only
# these three are substituted, so nothing else in the manifest (an image tag, a
# shell-like token) is touched — matching the recipe's envsubst allow-list.
_MAX_NUM_SEQS_PLACEHOLDER = "${MAX_NUM_SEQS}"
_KV_CACHE_DTYPE_PLACEHOLDER = "${KV_CACHE_DTYPE}"
_PREFIX_CACHING_PLACEHOLDER = "${PREFIX_CACHING_FLAG}"

# The worker's RBAC scopes it to the slipstream namespace's vllm-gpu Deployment.
_NAMESPACE = "slipstream"
_DEPLOYMENT = "vllm-gpu"

# A cold g5 must be provisioned by Karpenter, pull the ~10 GB image, and load the AWQ
# weights before the pod goes ready; the manifest's own progressDeadlineSeconds (1200)
# fails a stalled rollout, so the wait sits at the same ceiling.
_ROLLOUT_TIMEOUT_S = 1200.0

# The kubectl transport: run ``kubectl <args>`` with optional stdin, return stdout, and
# raise on a non-zero exit. Injected so the redeploy and scrape are testable against a
# fake — stateless and single-op, so a typed Callable, not a Protocol.
Kubectl = Callable[[Sequence[str], "str | None"], str]


class KubectlError(Exception):
    """A ``kubectl`` invocation that exited non-zero."""


def build_kubectl(*, binary: str = "kubectl") -> Kubectl:
    """Build the production :data:`Kubectl`: shell out to the ``kubectl`` binary.

    In-cluster the worker's ServiceAccount token and the in-cluster API endpoint are the
    ambient kubeconfig, so no auth is threaded here. A non-zero exit raises
    :class:`KubectlError` carrying the command and stderr, so a failed apply or a stalled
    rollout fails the point loudly rather than passing silently.

    :param binary: the kubectl executable name (overridable for tests).
    :return: the callable the redeploy and scrape send commands through.
    """

    def run(args: Sequence[str], stdin: str | None = None) -> str:
        completed = subprocess.run(
            [binary, *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise KubectlError(
                f"kubectl {' '.join(args)} exited {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        return completed.stdout

    return run


def render_gpu_manifest(manifest_text: str, *, engine_args: EngineArgs) -> str:
    """Substitute a point's three swept engine args into the GPU manifest text.

    The Python counterpart to the recipe's ``envsubst`` (``just _render-gpu-manifest``):
    only ``${MAX_NUM_SEQS}``, ``${KV_CACHE_DTYPE}``, and ``${PREFIX_CACHING_FLAG}`` are
    replaced, so nothing else in the manifest is touched. Rendering in Python keeps the
    worker image free of a gettext dependency.

    :param manifest_text: the raw ``k8s/vllm-gpu.yaml`` text with the placeholders.
    :param engine_args: the point's max-num-seqs, kv-cache token, and caching flag.
    :return: the manifest with the three placeholders rendered.
    """
    return (
        manifest_text.replace(_MAX_NUM_SEQS_PLACEHOLDER, str(engine_args.max_num_seqs))
        .replace(_KV_CACHE_DTYPE_PLACEHOLDER, engine_args.kv_engine_token)
        .replace(_PREFIX_CACHING_PLACEHOLDER, engine_args.prefix_caching_flag)
    )


def extract_deployment_doc(manifest_text: str, *, deployment: str = _DEPLOYMENT) -> str:
    """Return just the named Deployment document from a multi-document manifest.

    The worker's RBAC grants get/patch on ``deploy/vllm-gpu`` only — not the manifest's
    Namespace or Service — so the redeploy applies this one document, not the whole file
    (whose Namespace/Service the operator applied once at stack-up). Emitting one document
    keeps ``kubectl apply`` a get + patch of the Deployment the RBAC permits.

    :param manifest_text: the rendered manifest, one or more YAML documents.
    :param deployment: the Deployment's ``metadata.name`` to extract.
    :return: the Deployment document as YAML.
    :raise ValueError: when the manifest holds no Deployment of that name — a manifest
        that cannot be redeployed must fail loudly, not apply nothing.
    """
    for doc in yaml.safe_load_all(manifest_text):
        if (
            isinstance(doc, dict)
            and doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("name") == deployment
        ):
            return yaml.safe_dump(doc, sort_keys=False)
    raise ValueError(
        f"manifest holds no Deployment named {deployment!r}: cannot redeploy the point"
    )


def deploy_gpu_point(
    point: EnginePoint,
    *,
    grid: SweepGrid,
    manifest_text: str,
    kubectl: Kubectl,
    namespace: str = _NAMESPACE,
    deployment: str = _DEPLOYMENT,
    rollout_timeout_s: float = _ROLLOUT_TIMEOUT_S,
) -> None:
    """Redeploy the GPU for one engine point's knobs and wait for the rollout (Tier-1).

    Renders the point's swept knobs into the manifest, applies **only** the Deployment
    document (the get + patch the worker's RBAC allows), then blocks on the rollout so
    the point's ladder never runs against a half-rolled engine. ``kubectl`` raises on a
    non-zero exit, so a failed apply or a stalled rollout aborts the point loudly.

    :param point: the engine-knob point to redeploy.
    :param grid: the validated grid the point's manifest knobs are resolved from.
    :param manifest_text: the raw ``k8s/vllm-gpu.yaml`` text with the placeholders.
    :param kubectl: the transport the apply and rollout wait are issued through.
    :param namespace: the namespace the Deployment lives in.
    :param deployment: the Deployment's name.
    :param rollout_timeout_s: the ceiling the rollout wait trips at.
    """
    engine_args = get_engine_args(grid, point)
    rendered = render_gpu_manifest(manifest_text, engine_args=engine_args)
    deployment_doc = extract_deployment_doc(rendered, deployment=deployment)
    kubectl(["apply", "-n", namespace, "-f", "-"], deployment_doc)
    kubectl(
        [
            "rollout",
            "status",
            "-n",
            namespace,
            f"deployment/{deployment}",
            f"--timeout={int(rollout_timeout_s)}s",
        ],
        None,
    )


def scrape_ceiling(
    point: EnginePoint,
    *,
    kubectl: Kubectl,
    namespace: str = _NAMESPACE,
    deployment: str = _DEPLOYMENT,
) -> str:
    """Read the redeployed engine's predicted concurrency ceiling from its startup log.

    Reads ``kubectl logs deploy/vllm-gpu`` (one replica, so the log is the pod just
    rolled out) and returns the last ceiling line vLLM logged. The value is a
    VRAM/KV-budget upper bound, not a measured ceiling (ADR-0009) — a cross-check the
    caller records, not the reported ceiling.

    :param point: the engine-knob point whose engine was redeployed (for the message).
    :param kubectl: the transport the log read is issued through.
    :param namespace: the namespace the Deployment lives in.
    :param deployment: the Deployment whose pod log is read.
    :return: the ceiling line, e.g. ``Maximum concurrency for 4,096 tokens per
        request: 10.30x``.
    :raise CeilingScrapeError: when the log carries no ceiling line — the engine reported
        none, so the point fails loudly instead of measuring its ladder against a garbage
        ceiling (ADR-0015).
    """
    logs = kubectl(["logs", "-n", namespace, f"deployment/{deployment}"], None)
    matches = _CEILING_PATTERN.findall(logs)
    if not matches:
        raise CeilingScrapeError(
            f"no concurrency ceiling in the vLLM startup log for point "
            f"{point.slug()}: the engine reported none, so its Tier-2 ladder must "
            f"not run against a garbage ceiling (ADR-0015)"
        )
    return matches[-1]
