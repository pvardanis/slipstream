"""The orchestration CLI: assemble a point's SweepContext from files.

Each cell's config digest folds in ``model.yaml``'s bytes and the vLLM image ref read from
the manifest; ``build_sweep_context`` carries that serving config in the context the flow
runs against, so the CLI's context matches a direct :func:`read_serving_config`. The
``point-sweep`` command's body is driven with the AWS/Prefect boundary faked, so the
context it hands the flow and the pointers it echoes are covered without a live run.
"""

import re
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from slipstream_bench.orchestration import cli as cli_module
from slipstream_bench.orchestration.cli import app, build_sweep_context
from slipstream_bench.orchestration.digest import ServingConfig, read_serving_config
from slipstream_bench.orchestration.flows.point_sweep import SweepContext

# Typer colours an option name when a terminal forces colour (CI does), rendering
# ``--results-dir`` with a reset between the dashes so the literal hides from a
# substring check. Strip the escapes before asserting on text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return _ANSI.sub("", output)


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


def test_build_sweep_context_carries_the_serving_config(tmp_path: Path) -> None:
    model_yaml, _grid_yaml, manifest = _write_inputs(tmp_path)
    serving = read_serving_config(model_yaml=model_yaml, vllm_manifest=manifest)

    context = build_sweep_context(
        run_id="run1/mns64",
        point_slug="mns64",
        instance_id="i-1",
        image_ref="repo:tag",
        bucket="bench-bucket",
        model="Qwen/Qwen2.5-0.5B-Instruct",
        serving=serving,
    )

    assert context.serving == serving
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
            "s3://bench-bucket/knob-sweep/run1/mns64/pshare10_burst1.0_mc64.json",
            "s3://bench-bucket/knob-sweep/run1/mns64/pshare50_burst1.0_mc64.json",
        ]

    monkeypatch.setattr(cli_module, "run_point_sweep", _fake_run)

    result = CliRunner().invoke(
        app,
        [
            "point-sweep",
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
        ],
    )

    assert result.exit_code == 0, result.output
    assert "pshare10_burst1.0_mc64.json" in result.output
    assert "pshare50_burst1.0_mc64.json" in result.output
    context = cast(SweepContext, captured["context"])
    assert context.model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert context.serving == ServingConfig(
        model_yaml=_MODEL_YAML, vllm_image_ref="vllm/vllm-openai:v0.6.0"
    )


def test_point_sweep_rejects_a_results_dir_option(tmp_path: Path) -> None:
    # The worker's per-point download dir is an internal TemporaryDirectory, not an
    # operator-supplied path: the command must carry no --results-dir option to re-open.
    model_yaml, grid_yaml, manifest = _write_inputs(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "point-sweep",
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

    assert result.exit_code != 0
    assert "No such option: --results-dir" in _plain(result.output)
