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
from pathlib import Path

import yaml

# The container in k8s/vllm-gpu.yaml whose image is the serving engine build; a
# sidecar's image must never be read as the vLLM ref folded into the digest.
_VLLM_CONTAINER = "vllm"


class DigestError(Exception):
    """A config input the deep digest cannot be computed over."""


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
    try:
        text = manifest.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise DigestError(f"vLLM manifest not found: {manifest}") from error
    except (OSError, UnicodeDecodeError) as error:
        message = f"vLLM manifest could not be read: {manifest}: {error}"
        raise DigestError(message) from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise DigestError(f"{manifest} is not valid YAML: {error}") from error
    for entry in _get_containers(data, manifest):
        if isinstance(entry, dict) and entry.get("name") == container:
            return _require_image(entry, container, manifest)
    raise DigestError(
        f"{manifest} has no '{container}' container to read the serving image ref from"
    )


def _get_containers(data: object, manifest: Path) -> list[object]:
    """Walk a Deployment manifest to its pod template's container list.

    :param data: the parsed manifest.
    :param manifest: the manifest path, for the error message.
    :return: the container entries.
    :raise DigestError: when the manifest is not the expected Deployment shape.
    """
    node: object = data
    for key in ("spec", "template", "spec", "containers"):
        if not isinstance(node, dict) or key not in node:
            raise DigestError(
                f"{manifest} is not a Deployment with spec.template.spec.containers"
            )
        node = node[key]
    if not isinstance(node, list):
        raise DigestError(f"{manifest} containers is not a list")
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
