"""Render a knob-sweep run's configuration as a markdown artifact body.

A run's configuration is the deep digest's inputs (ADR-0012): model.yaml and
sweep-grid.yaml verbatim, and k8s/vllm-gpu.yaml — of which the serving image ref and the
vLLM container's args are shown. The args are embedded verbatim, swept placeholders and all
(``${MAX_NUM_SEQS}``/``${KV_CACHE_DTYPE}``/``${PREFIX_CACHING_FLAG}``): the sweep-grid above
decodes which value each placeholder takes per engine point, and the point slug on the
Prefect graph names the point a redeploy ran, so the swept deltas are already read off the
graph — resolving the args into one near-identical block per point would drown that signal.
The rest of the manifest is boilerplate the digest does not hash, so it is not embedded. The
run is already versioned — the bench-client and orchestration image content-sha tags tie it
to their builds and the digest proves config identity across runs — but none is
human-inspectable. :func:`render_config_artifact` folds the version anchors and those config
inputs into one markdown body the flow publishes as a keyed artifact, so an operator reads
the exact configuration a run executed from the Prefect UI without the files being copied to
S3. The render itself is a pure function; the Prefect ``create_markdown_artifact`` call stays
in the flow.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from slipstream_bench.orchestration.digest import (
    DigestInputs,
    read_image_ref,
    read_vllm_args,
)

# The Prefect markdown-artifact publisher, injected so this module stays prefect-free and
# the publish path is covered against a fake. In production the flow passes
# ``prefect.artifacts.create_markdown_artifact``.
ArtifactPublisher = Callable[..., Any]

# Keyed so Prefect keeps a cross-run history/timeline of the config artifact.
_ARTIFACT_KEY = "knob-sweep-config"


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


def publish_config_artifact(
    *,
    run_id: str,
    images: ImageRefs,
    digest_inputs: DigestInputs,
    publish: ArtifactPublisher,
) -> None:
    """Read the run's config and publish its rendered body as a keyed artifact.

    :param run_id: the shared run the sweep's points nest under.
    :param images: the bench-client and orchestration image refs the run records.
    :param digest_inputs: the deep digest's three inputs (ADR-0012); its model and grid are
        read verbatim, its vLLM manifest for the serving image ref and args, and it computes
        the digest shown.
    :param publish: the markdown-artifact publisher (create_markdown_artifact in production).
    """
    manifest = digest_inputs.vllm_manifest
    markdown = render_config_artifact(
        run_id=run_id,
        bench_image_ref=images.bench,
        orch_image=images.orch,
        digest=digest_inputs.digest(),
        model_yaml_text=digest_inputs.model_yaml.read_text(encoding="utf-8"),
        grid_text=digest_inputs.sweep_grid.read_text(encoding="utf-8"),
        vllm_image_ref=read_image_ref(manifest),
        vllm_args_text="\n".join(f"- {arg}" for arg in read_vllm_args(manifest)),
    )
    publish(key=_ARTIFACT_KEY, markdown=markdown)


def render_config_artifact(
    *,
    run_id: str,
    bench_image_ref: str,
    orch_image: str,
    digest: str,
    model_yaml_text: str,
    grid_text: str,
    vllm_image_ref: str,
    vllm_args_text: str,
) -> str:
    """Fold a run's version anchors and config inputs into a markdown artifact body.

    :param run_id: the shared run the sweep's points nest under.
    :param bench_image_ref: the bench-client image ref; its content-sha tag ties the run to
        a git commit.
    :param orch_image: the orchestration image ref that drove the run.
    :param digest: the deep config digest proving config identity across runs (ADR-0012).
    :param model_yaml_text: the model.yaml body (model identity).
    :param grid_text: the sweep-grid.yaml body (swept knobs).
    :param vllm_image_ref: the vLLM serving image ref read from k8s/vllm-gpu.yaml.
    :param vllm_args_text: the vLLM container args as a yaml list, verbatim with the swept
        placeholders intact.
    :return: a markdown body — a version header (bench_image, orch_image, vllm_image,
        digest), then model.yaml, the grid, and the vLLM args fenced as yaml.
    """
    sections = [
        f"## knob-sweep config ({run_id})",
        f"- bench_image: `{bench_image_ref}`",
        f"- orch_image: `{orch_image}`",
        f"- vllm_image: `{vllm_image_ref}`",
        f"- digest: `{digest}`",
        _fenced("model.yaml", model_yaml_text),
        _fenced("sweep-grid.yaml", grid_text),
        _fenced("vllm args", vllm_args_text),
    ]
    return "\n\n".join(sections) + "\n"


def _fenced(title: str, body: str) -> str:
    """Render one config section as a titled, yaml-fenced markdown block."""
    return f"### {title}\n\n```yaml\n{body.rstrip()}\n```"
