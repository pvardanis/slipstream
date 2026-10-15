"""The config digest changes when — and only when — a cell's numbers would.

A cell's digest is a length-framed sha256 over the model definition, this engine point's
knobs and the cell's parameters as canonical JSON, the vLLM serving image ref, the
bench-client image ref, the dry-run flag, and the validity threshold. It must move when
any of those moves, stay put when only another point or a grid comment changes, and two
different splittings of the same bytes must not collide into one key. The vLLM image ref
lives in ``k8s/vllm-gpu.yaml``, not ``model.yaml``, so it is read from that manifest.
"""

import base64
from pathlib import Path
from typing import Any

import pytest
import yaml

from slipstream_bench.contract import SweepGrid, get_cell_basename
from slipstream_bench.orchestration.digest import (
    DigestError,
    ServingConfig,
    config_digest,
    read_dry_run,
    read_image_ref,
    read_serving_config,
    read_vllm_args,
)
from slipstream_bench.orchestration.flows.point_sweep import (
    SweepContext,
    get_cell_digest,
    list_point_cells,
)

_MODEL = b"model:\n  hfId: Qwen/Qwen3-8B-AWQ\n"
_IMAGE = "vllm/vllm-openai:v0.29.0"
_POINT = {
    "max_num_seqs": 64,
    "kv_engine_token": "fp8",
    "prefix_caching_flag": "--enable-prefix-caching",
}
_CELL = {"prefix_share": 10, "burstiness": 1.0, "max_concurrency": 64, "seed": 0}
_INPUTS: dict[str, Any] = {
    "model_yaml": _MODEL,
    "point_knobs": _POINT,
    "cell_params": _CELL,
    "vllm_image_ref": _IMAGE,
    "bench_image_ref": "bench:abc123",
    "dry_run": False,
    "max_error_rate": 0.05,
}


def _digest(**overrides: Any) -> str:
    return config_digest(**{**_INPUTS, **overrides})


def test_digest_is_a_sha256_hex_string() -> None:
    digest = _digest()

    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_digest_is_deterministic() -> None:
    assert _digest() == _digest()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("model_yaml", _MODEL + b"  revision: deadbeef\n"),
        (
            "point_knobs",
            {**_POINT, "prefix_caching_flag": "--no-enable-prefix-caching"},
        ),
        ("cell_params", {**_CELL, "seed": 1}),
        ("vllm_image_ref", "vllm/vllm-openai:v0.30.0"),
        ("bench_image_ref", "bench:def456"),
        ("dry_run", True),
        ("max_error_rate", 0.01),
    ],
)
def test_each_input_moves_the_digest(name: str, value: object) -> None:
    assert _digest(**{name: value}) != _digest()


def test_knob_key_order_does_not_move_the_digest() -> None:
    reordered = dict(reversed(list(_POINT.items())))

    assert _digest(point_knobs=reordered) == _digest()


def test_a_boundary_shift_between_adjacent_inputs_does_not_collide() -> None:
    # Without framing, ("ab", "c") and ("a", "bc") would hash the same stream.
    left = _digest(vllm_image_ref="ab", bench_image_ref="c")
    right = _digest(vllm_image_ref="a", bench_image_ref="bc")

    assert left != right


def _encode_args(args: str) -> str:
    return base64.b64encode(args.encode("utf-8")).decode("ascii")


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("", False),
        ("--dry-run", True),
        ("--api-key-env OPENAI_KEY --dry-run", True),
        ("--api-key-env OPENAI_KEY", False),
        ("--dry-run --no-dry-run", False),
        ("--no-dry-run --dry-run", True),
    ],
)
def test_read_dry_run_reads_the_last_dry_run_flag(args: str, expected: bool) -> None:
    sweep_args_b64 = _encode_args(args) if args else ""

    assert read_dry_run(sweep_args_b64) is expected


def test_read_dry_run_rejects_args_that_are_not_base64() -> None:
    with pytest.raises(DigestError, match="base64"):
        read_dry_run("not base64!")


def test_read_serving_config_reads_the_model_bytes_and_vllm_image(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model.yaml"
    model.write_bytes(_MODEL)
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text(
        "spec:\n  template:\n    spec:\n      containers:\n"
        f"        - name: vllm\n          image: {_IMAGE}\n"
    )

    serving = read_serving_config(model_yaml=model, vllm_manifest=manifest)

    assert serving == ServingConfig(model_yaml=_MODEL, vllm_image_ref=_IMAGE)


@pytest.mark.parametrize(
    ("model_yaml", "vllm_image_ref"), [(b"", _IMAGE), (_MODEL, " ")]
)
def test_serving_config_rejects_a_blank_input(
    model_yaml: bytes, vllm_image_ref: str
) -> None:
    with pytest.raises(ValueError, match="ServingConfig"):
        ServingConfig(model_yaml=model_yaml, vllm_image_ref=vllm_image_ref)


# One prefix-caching-on point (mns64, fp8) and one off point, two cells on the on arm.
_GRID = """\
# the knob grid
tier1:
  max_num_seqs: [64]
  kv_cache_dtype: [fp8]
  prefix_caching:
    "on":
      flag: --enable-prefix-caching
      prefix_share: [10, 50]
    "off":
      flag: --no-enable-prefix-caching
      prefix_share: [0]
tier2:
  max_concurrency: [64]
  burstiness: 1.0
load:
  total_len: 1000
  num_prompts: 500
  num_prefixes: 5
  output_len: 128
  align_blocks: 0
  request_rate: 8
  seed: 0
  goodput: ["ttft:1000", "tpot:50"]
"""

_POINT_SLUG = "mns64_kvfp8_pcon"


def _context(**overrides: Any) -> SweepContext:
    fields: dict[str, Any] = {
        "run_id": f"run1/{_POINT_SLUG}",
        "point_slug": _POINT_SLUG,
        "serving": ServingConfig(model_yaml=_MODEL, vllm_image_ref=_IMAGE),
        "instance_id": "i-1",
        "image_ref": "bench:abc123",
        "bucket": "bench-bucket",
        "model": "Qwen/Qwen3-8B-AWQ",
    }
    return SweepContext(**{**fields, **overrides})


def _cell_digests(
    grid_text: str, context: SweepContext | None = None
) -> dict[str, str]:
    """Each of the point's cells' digest, by cell name, under ``grid_text``."""
    grid = SweepGrid.model_validate(yaml.safe_load(grid_text))
    context = context or _context()
    return {
        get_cell_basename(cell): get_cell_digest(grid, context, cell)
        for cell in list_point_cells(grid, context)
    }


def test_a_point_has_one_digest_per_cell() -> None:
    digests = _cell_digests(_GRID)

    assert len(digests) == 2
    assert len(set(digests.values())) == 2


def test_adding_another_point_leaves_this_points_digests_unchanged() -> None:
    grown = _GRID.replace("max_num_seqs: [64]", "max_num_seqs: [64, 128]")

    assert _cell_digests(grown) == _cell_digests(_GRID)


def test_editing_a_grid_comment_leaves_this_points_digests_unchanged() -> None:
    edited = _GRID.replace("# the knob grid", "# the knob grid, reworded")

    assert _cell_digests(edited) == _cell_digests(_GRID)


def test_another_points_knobs_leave_this_points_digests_unchanged() -> None:
    edited = _GRID.replace(
        "flag: --no-enable-prefix-caching", "flag: --no-enable-prefix-caching --x"
    )

    assert _cell_digests(edited) == _cell_digests(_GRID)


def test_this_points_knobs_move_every_cell_digest() -> None:
    edited = _GRID.replace(
        "flag: --enable-prefix-caching", "flag: --enable-prefix-caching --x"
    )
    before, after = _cell_digests(_GRID), _cell_digests(edited)

    assert all(after[name] != before[name] for name in before)


def test_a_cell_parameter_moves_the_cell_digest() -> None:
    edited = _GRID.replace("seed: 0", "seed: 1")
    before, after = _cell_digests(_GRID), _cell_digests(edited)

    assert all(after[name] != before[name] for name in before)


@pytest.mark.parametrize(
    "overrides",
    [
        {"image_ref": "bench:def456"},
        {"serving": ServingConfig(model_yaml=_MODEL, vllm_image_ref="vllm:v0.30.0")},
        {"serving": ServingConfig(model_yaml=_MODEL + b"#", vllm_image_ref=_IMAGE)},
        {"sweep_args_b64": _encode_args("--dry-run")},
    ],
)
def test_a_run_wide_input_moves_every_cell_digest(overrides: dict[str, Any]) -> None:
    before = _cell_digests(_GRID)
    after = _cell_digests(_GRID, _context(**overrides))

    assert all(after[name] != before[name] for name in before)


def test_the_api_key_env_name_leaves_the_digest_unchanged() -> None:
    keyed = _context(sweep_args_b64=_encode_args("--api-key-env OPENAI_KEY"))

    assert _cell_digests(_GRID, keyed) == _cell_digests(_GRID)


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


def test_read_image_ref_reads_from_a_multi_document_manifest(tmp_path: Path) -> None:
    # k8s/vllm-gpu.yaml bundles the Deployment and its Service, `---`-separated; the
    # image ref is read from the Deployment document past the others.
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: vllm\n"
        "          image: vllm/vllm-openai:v0.29.0\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "spec:\n"
        "  selector:\n"
        "    app: vllm-gpu\n"
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


def test_read_vllm_args_reads_the_vllm_container_args(tmp_path: Path) -> None:
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text(
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: vllm\n"
        "          image: vllm/vllm-openai:v0.29.0\n"
        "          args:\n"
        "            - --model\n"
        "            - Qwen/Qwen3-8B-AWQ\n"
        "            - --max-num-seqs\n"
        '            - "${MAX_NUM_SEQS}"\n'
    )

    assert read_vllm_args(manifest) == [
        "--model",
        "Qwen/Qwen3-8B-AWQ",
        "--max-num-seqs",
        "${MAX_NUM_SEQS}",
    ]


def test_read_vllm_args_rejects_a_container_without_args(tmp_path: Path) -> None:
    manifest = tmp_path / "vllm-gpu.yaml"
    manifest.write_text(
        "spec:\n  template:\n    spec:\n      containers:\n        - name: vllm\n"
    )

    with pytest.raises(DigestError, match="args"):
        read_vllm_args(manifest)
