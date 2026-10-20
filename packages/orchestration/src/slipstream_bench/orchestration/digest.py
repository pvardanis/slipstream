"""The config digest that defines when a cached cell has gone stale.

A cell's digest covers exactly the inputs that change its numbers: the model definition,
this engine point's knobs and the cell's parameters, the vLLM serving image ref, the
bench-client image ref, the extra load-cell flags, and the validity threshold. The point's knobs and
the cell's parameters are hashed as canonical (sorted-key) JSON, not as the raw grid file,
so adding another engine point or editing a grid comment invalidates nothing, while a change
to one point's knobs invalidates only that point. ``model.yaml`` stays hashed as raw bytes:
a whitespace-only edit then re-runs a cell rather than reusing it, which fails safe — a
spurious re-measure wastes a GPU-minute, a spurious reuse silently serves a stale number.
The vLLM image ref is not in ``model.yaml`` — it lives in ``k8s/vllm-gpu.yaml`` by
convention — so it is read from that manifest: a vLLM bump changes the numbers and must
invalidate the cache. The extra load-cell flags (``--dry-run``, and any flag the host
appends after its own, which overrides them) are hashed as given, bar the credential-only
``--api-key-env`` name, which changes no number.
"""

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

# The container in k8s/vllm-gpu.yaml whose image is the serving engine build; a
# sidecar's image must never be read as the vLLM ref folded into the digest.
_VLLM_CONTAINER = "vllm"

# The load-cell flag naming the env var that holds the API key: a credential, so it and
# its value change no number and are left out of the digest.
_CREDENTIAL_FLAG = "--api-key-env"


class DigestError(Exception):
    """A config input the config digest cannot be computed over."""


@dataclass(frozen=True)
class ServingConfig:
    """The run-wide digest inputs read from files: the model and the vLLM serving image.

    Read once per run and carried with it, so every cell's digest folds the same bytes.

    :param model_yaml: the raw bytes of ``model.yaml`` (model identity).
    :param vllm_image_ref: the vLLM serving image ref from ``k8s/vllm-gpu.yaml``.
    """

    model_yaml: bytes
    vllm_image_ref: str

    def __post_init__(self) -> None:
        """Reject an empty model or a blank image ref, which would key every cell alike.

        Frozen, so validating on construction makes the value valid for its whole life.
        """
        if not self.model_yaml:
            raise ValueError("ServingConfig.model_yaml must be non-empty")
        if not self.vllm_image_ref.strip():
            raise ValueError("ServingConfig.vllm_image_ref must be a non-empty string")


def read_serving_config(*, model_yaml: Path, vllm_manifest: Path) -> ServingConfig:
    """Read the model bytes and the vLLM serving image ref a run's digests fold in.

    :param model_yaml: ``model.yaml`` (model identity).
    :param vllm_manifest: ``k8s/vllm-gpu.yaml``, holding the serving image ref.
    :return: the run's serving config.
    :raise DigestError: when the vLLM manifest cannot yield a serving image ref.
    """
    return ServingConfig(
        model_yaml=model_yaml.read_bytes(),
        vllm_image_ref=read_image_ref(vllm_manifest),
    )


def config_digest(
    *,
    model_yaml: bytes,
    point_knobs: Mapping[str, object],
    cell_params: Mapping[str, object],
    vllm_image_ref: str,
    bench_image_ref: str,
    sweep_flags: Sequence[str],
    max_error_rate: float,
) -> str:
    """Hash every input that changes a cell's numbers into one stale-cache key.

    Each input is length-framed before hashing so a byte shifted across the boundary of
    two adjacent inputs cannot forge a different split into the same digest.

    :param model_yaml: the raw bytes of ``model.yaml`` (model identity).
    :param point_knobs: this engine point's knobs, hashed as canonical JSON.
    :param cell_params: the cell's parameters, hashed as canonical JSON.
    :param vllm_image_ref: the vLLM serving image ref from ``k8s/vllm-gpu.yaml``.
    :param bench_image_ref: the bench-client image ref the cell runs.
    :param sweep_flags: the extra load-cell flags the cell runs with, bar the credential
        name (:func:`read_sweep_flags`).
    :param max_error_rate: the validity threshold the cell's result is gated on.
    :return: the 64-char hex sha256 over the framed inputs.
    """
    parts = (
        model_yaml,
        _canonical_json(point_knobs),
        _canonical_json(cell_params),
        vllm_image_ref.encode("utf-8"),
        bench_image_ref.encode("utf-8"),
        _canonical_json(list(sweep_flags)),
        _canonical_json(max_error_rate),
    )
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(str(len(part)).encode("ascii"))
        hasher.update(b":")
        hasher.update(part)
    return hasher.hexdigest()


def read_sweep_flags(sweep_args_b64: str) -> tuple[str, ...]:
    """Read the extra load-cell flags a cell runs with, bar the credential name.

    The flags reach the host base64-encoded and are word-split there (``bench-sweep.sh``),
    which appends them after its own flags, so any of them — ``--dry-run``, or a repeat of
    a host flag, which overrides it — can change a cell's numbers and is kept. Only the
    ``--api-key-env`` credential name and its value are dropped.

    :param sweep_args_b64: the extra ``load-cell`` flags, base64-encoded (empty for none).
    :return: the flags in the order given, the credential name removed.
    :raise DigestError: when the flags are not valid base64, or hold a line break the host
        would drop everything after.
    """
    try:
        decoded = base64.b64decode(sweep_args_b64, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise DigestError(
            f"sweep args {sweep_args_b64!r} are not valid base64: {error}"
        ) from error
    if "\n" in decoded.rstrip("\n"):
        raise DigestError(
            f"sweep args {decoded!r} hold a line break; the host reads only the first "
            "line, so pass every flag on one line"
        )
    flags: list[str] = []
    tokens = iter(decoded.split())
    for token in tokens:
        if token == _CREDENTIAL_FLAG:
            next(tokens, None)
        elif not token.startswith(f"{_CREDENTIAL_FLAG}="):
            flags.append(token)
    return tuple(flags)


def _canonical_json(value: object) -> bytes:
    """Encode a value as canonical JSON: sorted keys, compact, NaN rejected."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


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


def read_vllm_env(
    manifest: Path, *, container: str = _VLLM_CONTAINER
) -> dict[str, str]:
    """Read the serving-engine container's literal env values out of the vLLM manifest.

    Only entries with a literal ``value`` are read; an entry sourced through
    ``valueFrom`` (a Secret, say) has no value in the manifest and is left out.

    :param manifest: the ``k8s/vllm-gpu.yaml`` Deployment manifest.
    :param container: the container name whose env is the serving engine's.
    :return: each literal env entry's name and value, as strings.
    :raise DigestError: when the manifest is missing, unreadable, not valid YAML, or holds
        no such container.
    """
    env = _find_container(manifest, container).get("env")
    entries = env if isinstance(env, list) else []
    return {
        str(entry["name"]): str(entry["value"])
        for entry in entries
        if isinstance(entry, dict) and "name" in entry and "value" in entry
    }


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
