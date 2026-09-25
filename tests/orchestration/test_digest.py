"""The deep config digest changes when — and only when — a cached number would.

ADR-0012: "stale" is a ``sha256(model.yaml + sweep-grid.yaml + vLLM image ref)``.
The digest must move when any of the three inputs moves (a re-quantized model, an
edited grid, a bumped vLLM image), and two different (model, grid) splittings of the
same bytes must not collide into one key. The image ref lives in ``k8s/vllm-gpu.yaml``,
not ``model.yaml``, so it is folded in explicitly and read from that manifest.
"""

from pathlib import Path

import pytest

from slipstream_bench.orchestration.digest import (
    DigestError,
    config_digest,
    read_image_ref,
)

_MODEL = b"model:\n  hfId: Qwen/Qwen3-8B-AWQ\n"
_GRID = b"tier1:\n  max_num_seqs: [16, 32]\n"
_IMAGE = "vllm/vllm-openai:v0.29.0"


def _digest() -> str:
    return config_digest(model_yaml=_MODEL, sweep_grid_yaml=_GRID, image_ref=_IMAGE)


def test_digest_is_a_sha256_hex_string() -> None:
    digest = _digest()

    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_digest_is_deterministic() -> None:
    assert _digest() == _digest()


def test_a_changed_model_moves_the_digest() -> None:
    other = config_digest(
        model_yaml=_MODEL + b"  revision: deadbeef\n",
        sweep_grid_yaml=_GRID,
        image_ref=_IMAGE,
    )

    assert other != _digest()


def test_a_changed_grid_moves_the_digest() -> None:
    other = config_digest(
        model_yaml=_MODEL,
        sweep_grid_yaml=_GRID + b"  kv_cache_dtype: [fp8]\n",
        image_ref=_IMAGE,
    )

    assert other != _digest()


def test_a_bumped_image_ref_moves_the_digest() -> None:
    other = config_digest(
        model_yaml=_MODEL, sweep_grid_yaml=_GRID, image_ref="vllm/vllm-openai:v0.30.0"
    )

    assert other != _digest()


def test_a_boundary_shift_between_model_and_grid_does_not_collide() -> None:
    # Without framing, ("ab", "c") and ("a", "bc") would hash the same stream.
    left = config_digest(model_yaml=b"ab", sweep_grid_yaml=b"c", image_ref=_IMAGE)
    right = config_digest(model_yaml=b"a", sweep_grid_yaml=b"bc", image_ref=_IMAGE)

    assert left != right


def test_read_image_ref_reads_the_vllm_container_image(tmp_path: Path) -> None:
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text(
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: vllm\n"
        "          image: vllm/vllm-openai:v0.29.0\n"
    )

    assert read_image_ref(manifest) == "vllm/vllm-openai:v0.29.0"


def test_read_image_ref_rejects_a_manifest_without_the_container(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text(
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: sidecar\n"
        "          image: busybox:1\n"
    )

    with pytest.raises(DigestError, match="vllm"):
        read_image_ref(manifest)


def test_read_image_ref_rejects_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(DigestError, match="not found"):
        read_image_ref(tmp_path / "absent.yaml")


def test_read_image_ref_rejects_a_container_without_an_image(tmp_path: Path) -> None:
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text(
        "spec:\n  template:\n    spec:\n      containers:\n        - name: vllm\n"
    )

    with pytest.raises(DigestError, match="image"):
        read_image_ref(manifest)


def test_read_image_ref_rejects_invalid_yaml(tmp_path: Path) -> None:
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text("spec: [unterminated\n")

    with pytest.raises(DigestError, match="not valid YAML"):
        read_image_ref(manifest)


def test_read_image_ref_rejects_a_non_deployment_shape(tmp_path: Path) -> None:
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text("kind: ConfigMap\ndata:\n  foo: bar\n")

    with pytest.raises(DigestError, match="Deployment"):
        read_image_ref(manifest)


def test_read_image_ref_rejects_a_non_list_containers(tmp_path: Path) -> None:
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text("spec:\n  template:\n    spec:\n      containers: notalist\n")

    with pytest.raises(DigestError, match="not a list"):
        read_image_ref(manifest)
