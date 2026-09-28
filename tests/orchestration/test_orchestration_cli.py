"""The orchestration CLI: assemble a point's SweepContext (digest and all) from files.

ADR-0012: the deep config digest is ``sha256(model.yaml + sweep-grid.yaml + vLLM image
ref)``. ``build_sweep_context`` reads those three and folds them into the context the
flow runs against, so the CLI's context matches a direct :func:`config_digest`. The
``point-sweep`` command's body is driven with the AWS/Prefect boundary faked, so the
context it hands the flow and the pointers it echoes are covered without a live run.
"""

from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from slipstream_bench.orchestration import cli as cli_module
from slipstream_bench.orchestration.cli import app, build_sweep_context
from slipstream_bench.orchestration.digest import DigestInputs, config_digest
from slipstream_bench.orchestration.flow import SweepContext

_MODEL_YAML = b"model:\n  hfId: Qwen/Qwen2.5-0.5B-Instruct\n"
_GRID_YAML = b"engine_points: []\n"
_MANIFEST = """
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:v0.6.0
"""


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    model_yaml = tmp_path / "model.yaml"
    grid_yaml = tmp_path / "sweep-grid.yaml"
    manifest = tmp_path / "vllm-gpu.yaml"
    model_yaml.write_bytes(_MODEL_YAML)
    grid_yaml.write_bytes(_GRID_YAML)
    manifest.write_text(_MANIFEST, encoding="utf-8")
    return model_yaml, grid_yaml, manifest


def test_build_sweep_context_folds_the_deep_digest(tmp_path: Path) -> None:
    model_yaml, grid_yaml, manifest = _write_inputs(tmp_path)

    context = build_sweep_context(
        run_id="run1/mns64",
        point_slug="mns64",
        instance_id="i-1",
        image_ref="repo:tag",
        bucket="bench-bucket",
        model="Qwen/Qwen2.5-0.5B-Instruct",
        digest_inputs=DigestInputs(
            model_yaml=model_yaml,
            sweep_grid=grid_yaml,
            vllm_manifest=manifest,
        ),
    )

    assert context.digest == config_digest(
        model_yaml=_MODEL_YAML,
        sweep_grid_yaml=_GRID_YAML,
        image_ref="vllm/vllm-openai:v0.6.0",
    )
    assert context.run_id == "run1/mns64"
    assert context.point_slug == "mns64"
    assert context.bucket == "bench-bucket"


def test_point_sweep_command_is_registered() -> None:
    names = [command.name for command in app.registered_commands]

    assert "point-sweep" in names


def test_point_sweep_echoes_each_cell_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_yaml, grid_yaml, manifest = _write_inputs(tmp_path)
    monkeypatch.setattr(cli_module, "build_ssm_client", lambda _region: object())
    monkeypatch.setattr(cli_module, "build_s3_client", lambda _region: object())
    monkeypatch.setattr(cli_module, "cell_task", lambda _bucket, *, retries=0: object())
    captured: dict[str, object] = {}

    def _fake_run(**kwargs: object) -> list[str]:
        captured.update(kwargs)
        return [
            "s3://bench-bucket/sweeps/run1/mns64/pshare10_burst1.0_mc64.json",
            "s3://bench-bucket/sweeps/run1/mns64/pshare50_burst1.0_mc64.json",
        ]

    monkeypatch.setattr(cli_module, "run_point_sweep", _fake_run)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(grid_yaml),
            "--run-id",
            "run1/mns64",
            "--point-slug",
            "mns64",
            "--instance-id",
            "i-1",
            "--region",
            "us-east-1",
            "--bucket",
            "bench-bucket",
            "--image-ref",
            "repo:tag",
            "--model-yaml",
            str(model_yaml),
            "--sweep-grid",
            str(grid_yaml),
            "--vllm-manifest",
            str(manifest),
            "--results-dir",
            str(tmp_path / "results"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "pshare10_burst1.0_mc64.json" in result.output
    assert "pshare50_burst1.0_mc64.json" in result.output
    context = cast(SweepContext, captured["context"])
    assert context.model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert context.digest == config_digest(
        model_yaml=_MODEL_YAML,
        sweep_grid_yaml=_GRID_YAML,
        image_ref="vllm/vllm-openai:v0.6.0",
    )
