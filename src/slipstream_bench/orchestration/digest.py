"""The deep config digest that defines when a cached cell has gone stale.

ADR-0012: two runs with the same engine-point slug but a different model or grid are
otherwise indistinguishable, so resume is keyed on a
``sha256(model.yaml + sweep-grid.yaml + vLLM image ref)``. ``model.yaml`` pins the
model identity and the grid pins the swept knobs, but neither pins the vLLM serving
image — that lives in ``k8s/vllm-gpu.yaml`` by convention — so the image ref is folded
in explicitly: a vLLM bump changes the numbers and must invalidate the cache. The raw
bytes of each file are hashed (not a parsed, normalised form): a whitespace-only edit
then re-runs a cell rather than reusing it, which fails safe — a spurious re-measure
wastes a GPU-minute, a spurious reuse silently serves a stale number.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

# The container in k8s/vllm-gpu.yaml whose image is the serving engine build; a
# sidecar's image must never be read as the vLLM ref folded into the digest.
_VLLM_CONTAINER = "vllm"


class DigestError(Exception):
    """A config input the deep digest cannot be computed over."""


@dataclass(frozen=True)
class DigestInputs:
    """The three files the deep config digest is computed over (ADR-0012).

    They travel together — a digest is only ever taken over all three — so they are
    bundled rather than threaded as separate parameters.

    :param model_yaml: ``model.yaml`` (model identity).
    :param sweep_grid: ``sweep-grid.yaml`` (swept knobs).
    :param vllm_manifest: ``k8s/vllm-gpu.yaml``, holding the serving image ref.
    """

    model_yaml: Path
    sweep_grid: Path
    vllm_manifest: Path

    def digest(self) -> str:
        """Fold the three inputs into the point's deep config digest.

        :return: the 64-char hex sha256 over the model bytes, grid bytes, and the
            serving image ref read from the vLLM manifest.
        :raise DigestError: when the vLLM manifest cannot yield a serving image ref.
        """
        return config_digest(
            model_yaml=self.model_yaml.read_bytes(),
            sweep_grid_yaml=self.sweep_grid.read_bytes(),
            image_ref=read_image_ref(self.vllm_manifest),
        )


def config_digest(*, model_yaml: bytes, sweep_grid_yaml: bytes, image_ref: str) -> str:
    """Hash the model, grid, and serving-image inputs into one stale-cache key.

    Each input is length-framed before hashing so a byte shifted across the
    model/grid boundary cannot forge a different split into the same digest.

    :param model_yaml: the raw bytes of ``model.yaml`` (model identity).
    :param sweep_grid_yaml: the raw bytes of ``sweep-grid.yaml`` (swept knobs).
    :param image_ref: the vLLM serving image ref from ``k8s/vllm-gpu.yaml``.
    :return: the 64-char hex sha256 over the three framed inputs.
    """
    hasher = hashlib.sha256()
    for part in (model_yaml, sweep_grid_yaml, image_ref.encode("utf-8")):
        hasher.update(str(len(part)).encode("ascii"))
        hasher.update(b":")
        hasher.update(part)
    return hasher.hexdigest()


def read_image_ref(manifest: Path, *, container: str = _VLLM_CONTAINER) -> str:
    """Read the serving-engine image ref out of the vLLM Deployment manifest.

    The image ref is not in ``model.yaml``; it is the ``image`` of the ``vllm``
    container in ``k8s/vllm-gpu.yaml``. Reading it here keeps the digest's one
    non-file input sourced from the manifest that actually pins it.

    :param manifest: the ``k8s/vllm-gpu.yaml`` Deployment manifest.
    :param container: the container name whose image is the serving engine.
    :return: the image ref, e.g. ``vllm/vllm-openai:v0.29.0``.
    :raise DigestError: when the manifest is missing, unreadable, not valid YAML,
        holds no such container, or that container declares no image.
    """
    return _require_image(_find_container(manifest, container), container, manifest)


def read_vllm_args(manifest: Path, *, container: str = _VLLM_CONTAINER) -> list[str]:
    """Read the serving-engine container's args out of the vLLM Deployment manifest.

    The args are the engine flags the ``vllm`` container runs with — ``--model``, the
    swept knobs rendered per point (``--max-num-seqs ${MAX_NUM_SEQS}``), and the rest. They
    are the manifest's redeploy shape, read here so a run's configuration can be shown
    without embedding the whole manifest body.

    :param manifest: the ``k8s/vllm-gpu.yaml`` Deployment manifest.
    :param container: the container name whose args are the serving engine's.
    :return: the container's args, each as a string.
    :raise DigestError: when the manifest is missing, unreadable, not valid YAML, holds no
        such container, or that container declares no args.
    """
    entry = _find_container(manifest, container)
    args = entry.get("args")
    if not isinstance(args, list) or not args:
        raise DigestError(
            f"{manifest} '{container}' container declares no args to read"
        )
    return [str(arg) for arg in args]


def _find_container(manifest: Path, container: str) -> dict[str, object]:
    """Find a named container entry in the vLLM Deployment manifest.

    :raise DigestError: when the manifest is missing, unreadable, not valid YAML, or holds
        no container of that name.
    """
    try:
        text = manifest.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise DigestError(f"vLLM manifest not found: {manifest}") from error
    except (OSError, UnicodeDecodeError) as error:
        message = f"vLLM manifest could not be read: {manifest}: {error}"
        raise DigestError(message) from error
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as error:
        raise DigestError(f"{manifest} is not valid YAML: {error}") from error
    for entry in _get_containers(documents, manifest):
        if isinstance(entry, dict) and entry.get("name") == container:
            return entry
    raise DigestError(f"{manifest} has no '{container}' container")


def _get_containers(documents: list[object], manifest: Path) -> list[object]:
    """Find the pod template's container list across a manifest's documents.

    ``k8s/vllm-gpu.yaml`` bundles the Deployment and its Service, ``---``-separated,
    so the Deployment document is picked out of the stream by its
    ``spec.template.spec.containers`` shape and the others are passed over.

    :param documents: the parsed manifest documents.
    :param manifest: the manifest path, for the error message.
    :return: the Deployment's container entries.
    :raise DigestError: when no document is a Deployment with that shape, or that
        document's containers are not a list.
    """
    for document in documents:
        containers = _template_containers(document)
        if containers is None:
            continue
        if not isinstance(containers, list):
            raise DigestError(f"{manifest} containers is not a list")
        return containers
    raise DigestError(
        f"{manifest} is not a Deployment with spec.template.spec.containers"
    )


def _template_containers(document: object) -> object | None:
    """Return a document's ``spec.template.spec.containers`` node, or None if absent."""
    node: object = document
    for key in ("spec", "template", "spec", "containers"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _require_image(entry: dict[str, object], container: str, manifest: Path) -> str:
    """Read a container entry's image ref, rejecting an absent or non-string one."""
    image = entry.get("image")
    if not isinstance(image, str) or not image.strip():
        raise DigestError(
            f"{manifest} '{container}' container declares no image ref to fold "
            f"into the digest"
        )
    return image
