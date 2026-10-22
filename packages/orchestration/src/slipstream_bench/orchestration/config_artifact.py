"""Render a knob-sweep or kv-ab run's configuration as a markdown artifact body.

A run's configuration is model.yaml and its workload file verbatim — the knob sweep's
sweep-grid.yaml or the KV cache A/B's kv-cache-extension-ab.yaml — and
k8s/vllm-gpu.yaml, of which the serving image ref and the vLLM container's args are
shown. The args are embedded verbatim, placeholders and all
(``${MAX_NUM_SEQS}``/``${KV_CACHE_DTYPE}``/``${PREFIX_CACHING_FLAG}``/
``${KV_CACHE_MEMORY_BYTES}``/``${KV_TRANSFER_CONFIG}``): the workload above and the flow
decode the values they take. In a knob sweep the grid names each engine point's knobs
(offload and the GPU KV pool cap are no grid knobs: the knob sweep always renders both
``None``, offload off and the pool uncapped), and the point slug on the Prefect graph
names the point a redeploy ran, so the swept deltas are already read off the graph —
resolving the args into one near-identical block per point would drown that signal. In a
kv-ab run the config's ``engine`` block names the knobs both arms deploy, prefix caching
is on in both, and each run's arm decides whether ``${KV_TRANSFER_CONFIG}`` renders the
LMCache connector. The rest of the manifest is boilerplate the digest does not hash, so
it is not embedded. The run is already versioned — the bench-client and orchestration
image content-sha tags tie it to their builds — but neither is human-inspectable.
:func:`render_config_artifact` folds the version anchors and those config inputs into
one markdown body the flow publishes as an artifact keyed by the flow, so an operator
reads the exact configuration a run executed from the Prefect UI without the files being
copied to S3. The render itself is a pure function; the Prefect
``create_markdown_artifact`` call stays in the flow.
"""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from slipstream_bench.orchestration.digest import read_image_ref, read_vllm_args

# The Prefect markdown-artifact publisher, injected so this module stays prefect-free and
# the publish path is covered against a fake. In production the flow passes
# ``prefect.artifacts.create_markdown_artifact``.
ArtifactPublisher = Callable[..., Any]

# The flows that publish a config artifact, each under its own ``<flow>-config`` key.
ConfigFlow = Literal["knob-sweep", "kv-ab"]

# The orchestration image bakes its own ref under this env var; a local or test build
# bakes none.
_ORCH_IMAGE_REF_ENV = "ORCH_IMAGE_REF"


@dataclass(frozen=True)
class ImageRefs:
    """The image refs a run records: the two builds whose code the run executes.

    :param bench: the bench-client image ref the cells run (content-sha tag ties the run to
        a commit).
    :param orch: the orchestration image ref that drove the sweep (``"unknown"`` when a local
        or test build baked no ref).
    """

    bench: str
    orch: str


def read_orch_image_ref(logger: logging.Logger | logging.LoggerAdapter[Any]) -> str:
    """Read the orchestration image's baked ref, or ``"unknown"`` when none is baked.

    The fallback is logged, so a cluster run whose env should carry the ref but doesn't
    leaves a trace rather than silently reading "unknown".

    :param logger: the run logger the fallback is recorded on.
    :return: the ``ORCH_IMAGE_REF`` value, or ``"unknown"``.
    """
    orch_ref = os.environ.get(_ORCH_IMAGE_REF_ENV)
    if not orch_ref:
        logger.info(
            '%s unset; recording the orchestration image as "unknown"',
            _ORCH_IMAGE_REF_ENV,
        )
        return "unknown"
    return orch_ref


def publish_config_artifact(
    *,
    flow: ConfigFlow,
    run_id: str,
    images: ImageRefs,
    model_yaml: Path,
    workload: Path,
    vllm_manifest: Path,
    publish: ArtifactPublisher,
) -> None:
    """Read the run's config and publish its rendered body as an artifact keyed by flow.

    Keyed ``<flow>-config``, so Prefect keeps each flow's cross-run history of it.

    :param flow: the flow's short name, ``knob-sweep`` or ``kv-ab``.
    :param run_id: the run the flow's cells nest under.
    :param images: the bench-client and orchestration image refs the run records.
    :param model_yaml: ``model.yaml``, read verbatim.
    :param workload: the flow's workload file, read verbatim and titled by its name.
    :param vllm_manifest: ``k8s/vllm-gpu.yaml``, read for the serving image ref and args.
    :param publish: the markdown-artifact publisher (create_markdown_artifact in production).
    """
    markdown = render_config_artifact(
        flow=flow,
        run_id=run_id,
        bench_image_ref=images.bench,
        orch_image=images.orch,
        model_yaml_text=model_yaml.read_text(encoding="utf-8"),
        workload_name=workload.name,
        workload_text=workload.read_text(encoding="utf-8"),
        vllm_image_ref=read_image_ref(vllm_manifest),
        vllm_args_text="\n".join(f"- {arg}" for arg in read_vllm_args(vllm_manifest)),
    )
    publish(key=f"{flow}-config", markdown=markdown)


def render_config_artifact(
    *,
    flow: ConfigFlow,
    run_id: str,
    bench_image_ref: str,
    orch_image: str,
    model_yaml_text: str,
    workload_name: str,
    workload_text: str,
    vllm_image_ref: str,
    vllm_args_text: str,
) -> str:
    """Fold a run's version anchors and config inputs into a markdown artifact body.

    :param flow: the flow's short name the heading names, ``knob-sweep`` or ``kv-ab``.
    :param run_id: the run the flow's cells nest under.
    :param bench_image_ref: the bench-client image ref; its content-sha tag ties the run to
        a git commit.
    :param orch_image: the orchestration image ref that drove the run.
    :param model_yaml_text: the model.yaml body (model identity).
    :param workload_name: the workload file's name, the title of its section.
    :param workload_text: the workload file's body (the swept or A/B knobs).
    :param vllm_image_ref: the vLLM serving image ref read from k8s/vllm-gpu.yaml.
    :param vllm_args_text: the vLLM container args as a yaml list, verbatim with the
        placeholders intact.
    :return: a markdown body — a version header (bench_image, orch_image, vllm_image),
        then model.yaml, the workload, and the vLLM args fenced as yaml.
    """
    sections = [
        f"## {flow} config ({run_id})",
        f"- bench_image: `{bench_image_ref}`",
        f"- orch_image: `{orch_image}`",
        f"- vllm_image: `{vllm_image_ref}`",
        _fenced("model.yaml", model_yaml_text),
        _fenced(workload_name, workload_text),
        _fenced("vllm args", vllm_args_text),
    ]
    return "\n\n".join(sections) + "\n"


def _fenced(title: str, body: str) -> str:
    """Render one config section as a titled, yaml-fenced markdown block."""
    return f"### {title}\n\n```yaml\n{body.rstrip()}\n```"
