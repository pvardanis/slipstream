"""The knob-sweep composition root: wire the four collaborators, drive the parent flow.

Exercises ``build_knob_sweep_collaborators`` — the factory that binds the injected
callables the parent flow sequences — against a fake kubectl and a faked point sweep and
redeploy-skip gate: no cluster, no AWS. Asserts the deploy applies the point's Deployment
and waits its rollout, the scrape logs the predicted ceiling and raises loud on an empty
one, each point sweep runs under its own results subdir and the sweep's nested run id, and
the pending gate is the negation of ``point_is_complete``. The ``knob-sweep`` command is
driven with the AWS/kubectl/flow boundary faked, so its wiring is covered without a run.
"""

import logging
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from botocore.exceptions import ClientError
from prefect import Task
from typer.testing import CliRunner

from slipstream_bench.orchestration import cli as cli_module
from slipstream_bench.orchestration.cli import (
    KnobSweepInputs,
    _point_has_pending_cells,
    _scrape_and_log_ceiling,
    _sweep_one_point,
    app,
    build_knob_sweep_collaborators,
)
from slipstream_bench.sweep.aggregation import CeilingScrapeError, EnginePoint
from slipstream_bench.sweep.grid import SweepGrid

_GRID = """
tier1:
  max_num_seqs: [64]
  kv_cache_dtype: [fp8]
  prefix_caching:
    "on":
      flag: --enable-prefix-caching
      prefix_share: [10]
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

_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-gpu
  namespace: slipstream
spec:
  template:
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:v0.6.0
          args:
            - --kv-cache-dtype
            - "${KV_CACHE_DTYPE}"
            - --max-num-seqs
            - "${MAX_NUM_SEQS}"
            - ${PREFIX_CACHING_FLAG}
"""

_CEILING_LOG = "Maximum concurrency for 4,096 tokens per request: 12.50x"
_POINT = EnginePoint(max_num_seqs=64, kv_cache_dtype="fp8", prefix_caching=True)
_OTHER_POINT = EnginePoint(
    max_num_seqs=128, kv_cache_dtype="fp16", prefix_caching=False
)


class _FakeKubectl:
    """Record every ``(argv, stdin)`` issued; serve canned logs to a ``logs`` call."""

    def __init__(self, logs: str = "") -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self._logs = logs

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        self.calls.append((list(args), stdin))
        return self._logs if "logs" in args else ""


def _grid() -> SweepGrid:
    return SweepGrid.model_validate(yaml.safe_load(_GRID))


def _inputs(results_dir: Path) -> KnobSweepInputs:
    manifest_path = results_dir / "vllm-gpu.yaml"
    manifest_path.write_text(_MANIFEST, encoding="utf-8")
    return KnobSweepInputs(
        grid=_grid(),
        grid_path=Path("sweep-grid.yaml"),
        manifest_path=manifest_path,
        results_dir=results_dir,
        run="run1",
        digest="deadbeef",
        instance_id="i-1",
        image_ref="repo:tag",
        bucket="bench-bucket",
        model="Qwen/Qwen2.5-0.5B-Instruct",
    )


def _collaborators(
    results_dir: Path, *, kubectl: _FakeKubectl
) -> tuple[Any, Any, Any, Any]:
    return build_knob_sweep_collaborators(
        _inputs(results_dir),
        kubectl=kubectl,
        ssm_client=object(),
        s3_client=object(),
        task=cast(Task[..., str], object()),
    )


def test_deploy_fn_applies_the_points_deployment_then_waits_the_rollout(
    tmp_path: Path,
) -> None:
    kubectl = _FakeKubectl()
    deploy_fn, _scrape, _sweep, _pending = _collaborators(tmp_path, kubectl=kubectl)

    deploy_fn(_POINT)

    assert kubectl.calls[0][0] == ["apply", "-n", "slipstream", "-f", "-"]
    assert kubectl.calls[1][0][:2] == ["rollout", "status"]


def test_scrape_logs_the_predicted_ceiling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    kubectl = _FakeKubectl(logs=_CEILING_LOG)

    with caplog.at_level(logging.INFO, logger="slipstream_bench.orchestration.cli"):
        _scrape_and_log_ceiling(_POINT, kubectl=kubectl)

    assert _POINT.slug() in caplog.text
    assert "12.50x" in caplog.text


def test_scrape_raises_loud_when_the_log_carries_no_ceiling() -> None:
    kubectl = _FakeKubectl(logs="INFO startup, no ceiling line")

    with pytest.raises(CeilingScrapeError, match=_POINT.slug()):
        _scrape_and_log_ceiling(_POINT, kubectl=kubectl)


def test_sweep_one_point_runs_under_the_points_run_id_and_results_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_run(**kwargs: Any) -> list[str]:
        calls.append(kwargs)
        return [f"ptr:{kwargs['context'].point_slug}"]

    monkeypatch.setattr(cli_module, "run_point_sweep", _fake_run)
    inputs = _inputs(tmp_path)

    def _sweep(point: EnginePoint) -> list[str]:
        return _sweep_one_point(
            point,
            inputs=inputs,
            ssm_client=object(),
            s3_client=object(),
            task=cast(Task[..., str], object()),
        )

    # Two distinct points: each nests under the sweep's shared run and lands in its own
    # per-point subdir, so their identically-named tier-2 cells never collide on disk.
    assert _sweep(_POINT) == [f"ptr:{_POINT.slug()}"]
    assert _sweep(_OTHER_POINT) == [f"ptr:{_OTHER_POINT.slug()}"]

    assert calls[0]["context"].run_id == f"run1/{_POINT.slug()}"
    assert calls[0]["results_dir"] == tmp_path / _POINT.slug()
    assert calls[1]["context"].run_id == f"run1/{_OTHER_POINT.slug()}"
    assert calls[1]["results_dir"] == tmp_path / _OTHER_POINT.slug()
    assert calls[0]["results_dir"] != calls[1]["results_dir"]


def test_has_pending_cells_is_the_negation_of_point_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)

    # A complete point has no pending cells; an incomplete one does.
    monkeypatch.setattr(cli_module, "point_is_complete", lambda *a, **k: True)
    assert _point_has_pending_cells(_POINT, inputs=inputs, s3_client=object()) is False
    monkeypatch.setattr(cli_module, "point_is_complete", lambda *a, **k: False)
    assert _point_has_pending_cells(_POINT, inputs=inputs, s3_client=object()) is True


def test_has_pending_cells_propagates_an_unfixable_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate negates point_is_complete, which raises on an S3 error re-running cannot
    # fix (a 403, a wrong bucket). The negation must not turn that raise into "pending":
    # a misconfigured unattended sweep aborts at the first probe, it does not loop.
    def _raising(*_a: Any, **_k: Any) -> bool:
        raise ClientError({"Error": {"Code": "403"}}, "HeadObject")

    monkeypatch.setattr(cli_module, "point_is_complete", _raising)
    inputs = _inputs(tmp_path)

    with pytest.raises(ClientError):
        _point_has_pending_cells(_POINT, inputs=inputs, s3_client=object())


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    model_yaml = tmp_path / "model.yaml"
    grid_yaml = tmp_path / "sweep-grid.yaml"
    manifest = tmp_path / "vllm-gpu.yaml"
    model_yaml.write_text(
        "model:\n  hfId: Qwen/Qwen2.5-0.5B-Instruct\n", encoding="utf-8"
    )
    grid_yaml.write_text(_GRID, encoding="utf-8")
    manifest.write_text(_MANIFEST, encoding="utf-8")
    return model_yaml, grid_yaml, manifest


@pytest.mark.parametrize("field", ["run", "digest", "bucket", "model"])
def test_empty_keying_input_is_rejected_before_the_redeploy(
    tmp_path: Path, field: str
) -> None:
    # An empty keying field would address cells under a broken key; rejecting it on
    # construction fails the sweep before its first ~20-minute GPU redeploy, not at the
    # S3 probe or silently.
    kwargs: dict[str, Any] = {
        "grid": _grid(),
        "grid_path": Path("sweep-grid.yaml"),
        "manifest_path": Path("vllm-gpu.yaml"),
        "results_dir": tmp_path,
        "run": "run1",
        "digest": "deadbeef",
        "instance_id": "i-1",
        "image_ref": "repo:tag",
        "bucket": "bench-bucket",
        "model": "Qwen/Qwen2.5-0.5B-Instruct",
    }
    kwargs[field] = ""

    with pytest.raises(ValueError, match=field):
        KnobSweepInputs(**kwargs)


def test_knob_sweep_command_is_registered() -> None:
    names = [command.name for command in app.registered_commands]

    assert "knob-sweep" in names


def test_knob_sweep_drives_the_grids_points_and_echoes_pointers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_yaml, grid_yaml, manifest = _write_inputs(tmp_path)
    monkeypatch.setattr(cli_module, "build_kubectl", lambda: _FakeKubectl())
    monkeypatch.setattr(cli_module, "build_ssm_client", lambda _region: object())
    monkeypatch.setattr(cli_module, "build_s3_client", lambda _region: object())
    task_args: dict[str, Any] = {}

    def _fake_cell_task(bucket: str, *, retries: int = 0) -> object:
        task_args.update(bucket=bucket, retries=retries)
        return object()

    monkeypatch.setattr(cli_module, "cell_task", _fake_cell_task)
    captured: dict[str, Any] = {}

    def _fake_run(**kwargs: Any) -> list[str]:
        captured.update(kwargs)
        return [
            "s3://bench-bucket/sweeps/run1/mns64_kvfp8_pcon/pshare10_burst1.0_mc64.json"
        ]

    monkeypatch.setattr(cli_module, "run_knob_sweep", _fake_run)

    result = CliRunner().invoke(
        app,
        [
            "knob-sweep",
            "--run-id",
            "run1",
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
            "--retries",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "pshare10_burst1.0_mc64.json" in result.output
    # The grid holds one engine point, so the sweep is driven over exactly it.
    assert [point.slug() for point in captured["points"]] == [_POINT.slug()]
    # All four collaborators reach the flow under their expected keywords, callable — so
    # a keyword-name mismatch or a dropped collaborator at the command->flow seam fails.
    for name in ("deploy_fn", "scrape_fn", "point_sweep_fn", "has_pending_cells"):
        assert callable(captured[name]), name
    # The cell task is built on the run's bucket with the command's retries threaded.
    assert task_args == {"bucket": "bench-bucket", "retries": 2}
