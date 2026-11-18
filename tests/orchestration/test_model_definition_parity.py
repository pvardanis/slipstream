"""The vLLM manifests serve the model ``model.yaml`` defines.

``model.yaml`` is the single definition of the served model (ADR-0008). The bench image
reads it directly; the bench Engine's template and the serving fleet's generated manifest
carry its values as literal vLLM args. These tests hold both manifests to it, so a model
change that misses a manifest fails here rather than serving a model the bench image's
tokenizer does not match.
"""

from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from slipstream_bench.contract.grid import KvLabel
from slipstream_bench.orchestration.grid import get_kv_engine_token

_REPO = Path(__file__).parents[2]
_MODEL_YAML = _REPO / "model.yaml"
_TEMPLATE = _REPO / "k8s" / "vllm-gpu.yaml"
_SERVING_MANIFEST = _REPO / "k8s" / "serving" / "vllm-serving.yaml"

# The model.yaml keys carried verbatim as vLLM args, and the flag each one sets.
_VERBATIM_ARGS = {
    "hfId": "--model",
    "revision": "--revision",
    "quantization": "--quantization",
}


def _read_model() -> dict[str, str]:
    """Return model.yaml's model block."""
    return yaml.safe_load(_MODEL_YAML.read_text(encoding="utf-8"))["model"]


def _read_kv_label() -> KvLabel:
    """Return model.yaml's KV cache dtype as the chart label the token mapping keys on."""
    dtype = _read_model()["kvCacheDtype"]
    labels: tuple[KvLabel, ...] = get_args(KvLabel)
    matches = [label for label in labels if label == dtype]
    assert matches, f"model.yaml kvCacheDtype {dtype!r} is not one of {labels}"
    return matches[0]


def _vllm_args(manifest: Path) -> list[str]:
    """Return the vllm container's args from a manifest's Deployment."""
    documents: list[dict[str, Any]] = list(
        yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
    )
    (deployment,) = [doc for doc in documents if doc["kind"] == "Deployment"]
    containers = deployment["spec"]["template"]["spec"]["containers"]
    (vllm,) = [container for container in containers if container["name"] == "vllm"]
    return vllm["args"]


def _arg_value(args: list[str], flag: str) -> str:
    """Return the value following ``flag`` in a container's args."""
    return args[args.index(flag) + 1]


@pytest.mark.parametrize(
    "manifest", [_TEMPLATE, _SERVING_MANIFEST], ids=lambda p: p.name
)
@pytest.mark.parametrize("key", sorted(_VERBATIM_ARGS))
def test_the_manifest_serves_the_defined_model(manifest: Path, key: str) -> None:
    args = _vllm_args(manifest)

    assert _arg_value(args, _VERBATIM_ARGS[key]) == _read_model()[key], (
        f"{manifest.relative_to(_REPO)} {_VERBATIM_ARGS[key]} differs from model.yaml "
        f"model.{key}"
    )


def test_the_serving_fleet_runs_the_defined_kv_cache_dtype() -> None:
    """The template renders its dtype per run; the serving fleet fixes it."""
    args = _vllm_args(_SERVING_MANIFEST)

    assert _arg_value(args, "--kv-cache-dtype") == get_kv_engine_token(_read_kv_label())
