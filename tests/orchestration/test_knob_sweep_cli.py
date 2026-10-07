"""The knob-sweep composition root: wire the four collaborators, drive the parent flow.

Exercises ``build_knob_sweep_collaborators`` — the factory that binds the injected
callables the parent flow sequences — against a fake kubectl and a faked point sweep and
redeploy-skip gate: no cluster, no AWS. Asserts the deploy applies the point's Deployment
and waits its rollout, the scrape logs the predicted ceiling and raises loud on an empty
one, each point sweep runs under its own results subdir and the sweep's nested run id, and
the pending gate is the negation of ``point_is_complete``. The ``knob-sweep`` command runs
the real ``knob_sweep_flow`` against an ephemeral Prefect server with the transports and
the sequencing driver faked, so the command->flow->driver wiring is covered without a
cluster; ``register-knob-sweep`` is driven with ``deploy`` faked, asserting the deployment
is registered as a module-path entrypoint on the process pool with no image build.
"""

import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from botocore.exceptions import ClientError
from prefect import Task
from prefect.testing.utilities import prefect_test_harness
from typer.testing import CliRunner

from slipstream_bench.contract import EnginePoint, SweepGrid
from slipstream_bench.orchestration import cli as cli_module
from slipstream_bench.orchestration.cli import app
from slipstream_bench.orchestration.cluster import CeilingScrapeError
from slipstream_bench.orchestration.flows import knob_sweep as knob_sweep_module
from slipstream_bench.orchestration.flows.knob_sweep import (
    KnobSweepInputs,
    _point_has_pending_cells,
    _scrape_and_log_ceiling,
    _sweep_one_point,
    build_knob_sweep_collaborators,
    knob_sweep_flow,
)
from tests.orchestration.cell_object_fakes import ServingCellS3

# Typer colours an option name when a terminal forces colour (CI does), rendering
# ``--results-dir`` with a reset between the dashes so the literal hides from a
# substring check. Strip the escapes before asserting on text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return _ANSI.sub("", output)


@pytest.fixture
def _harness() -> Iterator[None]:
    """Run the real ``knob_sweep_flow`` against an ephemeral Prefect server."""
    with prefect_test_harness():
        yield


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

_MULTI_GRID = """
tier1:
  max_num_seqs: [64, 128]
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


def _inputs(manifest_dir: Path) -> KnobSweepInputs:
    manifest_path = manifest_dir / "vllm-gpu.yaml"
    manifest_path.write_text(_MANIFEST, encoding="utf-8")
    return KnobSweepInputs(
        grid=_grid(),
        grid_path=Path("sweep-grid.yaml"),
        manifest_path=manifest_path,
        run="run1",
        digest="deadbeef",
        instance_id="i-1",
        image_ref="repo:tag",
        bucket="bench-bucket",
        model="Qwen/Qwen2.5-0.5B-Instruct",
    )


def _collaborators(
    manifest_dir: Path, *, kubectl: _FakeKubectl
) -> tuple[Any, Any, Any, Any]:
    return build_knob_sweep_collaborators(
        _inputs(manifest_dir),
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

    with caplog.at_level(
        logging.INFO, logger="slipstream_bench.orchestration.flows.knob_sweep"
    ):
        _scrape_and_log_ceiling(_POINT, kubectl=kubectl)

    assert _POINT.slug() in caplog.text
    assert "12.50x" in caplog.text


def test_scrape_raises_loud_when_the_log_carries_no_ceiling() -> None:
    kubectl = _FakeKubectl(logs="INFO startup, no ceiling line")

    with pytest.raises(CeilingScrapeError, match=_POINT.slug()):
        _scrape_and_log_ceiling(_POINT, kubectl=kubectl)


def test_sweep_one_point_runs_under_the_points_nested_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_run(**kwargs: Any) -> list[str]:
        calls.append(kwargs)
        return [f"ptr:{kwargs['context'].point_slug}"]

    monkeypatch.setattr(knob_sweep_module, "run_point_sweep", _fake_run)
    inputs = _inputs(tmp_path)

    def _sweep(point: EnginePoint) -> list[str]:
        return _sweep_one_point(
            point,
            inputs=inputs,
            ssm_client=object(),
            s3_client=object(),
            task=cast(Task[..., str], object()),
        )

    # Two distinct points: each nests under the sweep's shared run (<run>/<point-slug>),
    # so their cells are addressed and keyed apart.
    assert _sweep(_POINT) == [f"ptr:{_POINT.slug()}"]
    assert _sweep(_OTHER_POINT) == [f"ptr:{_OTHER_POINT.slug()}"]

    assert calls[0]["context"].run_id == f"run1/{_POINT.slug()}"
    assert calls[1]["context"].run_id == f"run1/{_OTHER_POINT.slug()}"


def test_has_pending_cells_is_the_negation_of_point_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)

    # A complete point has no pending cells; an incomplete one does.
    monkeypatch.setattr(knob_sweep_module, "point_is_complete", lambda *a, **k: True)
    assert _point_has_pending_cells(_POINT, inputs=inputs, s3_client=object()) is False
    monkeypatch.setattr(knob_sweep_module, "point_is_complete", lambda *a, **k: False)
    assert _point_has_pending_cells(_POINT, inputs=inputs, s3_client=object()) is True


def test_has_pending_cells_propagates_an_unfixable_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate negates point_is_complete, which raises on an S3 error re-running cannot
    # fix (a 403, a wrong bucket). The negation must not turn that raise into "pending":
    # a misconfigured unattended sweep aborts at the first probe, it does not loop.
    def _raising(*_a: Any, **_k: Any) -> bool:
        raise ClientError({"Error": {"Code": "403"}}, "HeadObject")

    monkeypatch.setattr(knob_sweep_module, "point_is_complete", _raising)
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
def test_empty_keying_input_is_rejected_before_the_redeploy(field: str) -> None:
    # An empty keying field would address cells under a broken key; rejecting it on
    # construction fails the sweep before its first ~20-minute GPU redeploy, not at the
    # S3 probe or silently.
    kwargs: dict[str, Any] = {
        "grid": _grid(),
        "grid_path": Path("sweep-grid.yaml"),
        "manifest_path": Path("vllm-gpu.yaml"),
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
    assert "register-knob-sweep" in names


def test_knob_sweep_flow_is_named_for_the_deployment() -> None:
    # The deployment and the parent flow run share this name; the point sweeps nest under
    # it in the Prefect UI, so a rename here silently breaks the registered deployment.
    assert knob_sweep_flow.name == "knob-sweep"


def _stub_transports(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the live transports the flow builds, recording the args it threads into them.

    Captures the cell task's build args and the region the boto3 clients are built on, so a
    test asserts the flow threads ``bucket``/``retries``/``region`` from its parameters into
    the transports rather than dropping or hardcoding them.
    """
    captures: dict[str, Any] = {"task_args": {}, "artifacts": [], "images": []}

    def _fake_cell_task(bucket: str, *, retries: int = 0) -> object:
        captures["task_args"] = {"bucket": bucket, "retries": retries}
        return object()

    def _fake_publish(*, key: str, markdown: str) -> None:
        # The terminal render task publishes the ceiling and cliff tables through this; recorded
        # so a test asserts both tables land off the render wiring.
        captures["artifacts"].append((key, markdown))

    def _fake_publish_image(*, image_url: str, key: str, description: str) -> None:
        # The render task publishes the two plots as image artifacts through this, each pointing
        # at its public S3 URL; recorded so a test asserts both plots land off the render wiring.
        captures["images"].append((key, image_url))

    def _fake_ssm(region: str) -> object:
        captures["ssm_region"] = region
        return object()

    def _fake_s3(region: str) -> object:
        captures["s3_region"] = region
        # Serve the run's cells so the terminal render task materializes and folds them off
        # S3 (ADR-0018), rather than an empty object the render's first download would break
        # on. Recorded so a test asserts the render ran against the flow-built client.
        s3 = ServingCellS3()
        captures["s3"] = s3
        return s3

    monkeypatch.setattr(knob_sweep_module, "build_kubectl", lambda: _FakeKubectl())
    monkeypatch.setattr(knob_sweep_module, "build_ssm_client", _fake_ssm)
    monkeypatch.setattr(knob_sweep_module, "build_s3_client", _fake_s3)
    monkeypatch.setattr(knob_sweep_module, "cell_task", _fake_cell_task)
    monkeypatch.setattr(knob_sweep_module, "create_markdown_artifact", _fake_publish)
    monkeypatch.setattr(knob_sweep_module, "create_image_artifact", _fake_publish_image)
    return captures


def test_knob_sweep_drives_the_grids_points_and_echoes_pointers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _harness: None
) -> None:
    model_yaml, grid_yaml, manifest = _write_inputs(tmp_path)
    captures = _stub_transports(monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_drive(**kwargs: Any) -> list[str]:
        captured.update(kwargs)
        return [
            "s3://bench-bucket/sweeps/run1/mns64_kvfp8_pcon/pshare10_burst1.0_mc64.json"
        ]

    monkeypatch.setattr(knob_sweep_module, "drive_knob_sweep", _fake_drive)

    # Capture the inputs the flow constructs, so the arms the grid does not carry — the
    # commercial tokenizer arm and the extra load-cell flags — are asserted to reach it.
    real_build = knob_sweep_module.build_knob_sweep_collaborators
    built_inputs: dict[str, Any] = {}

    def _capturing_build(inputs: KnobSweepInputs, **kwargs: Any) -> Any:
        built_inputs["inputs"] = inputs
        return real_build(inputs, **kwargs)

    monkeypatch.setattr(
        knob_sweep_module, "build_knob_sweep_collaborators", _capturing_build
    )

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
            "--retries",
            "2",
            "--commercial",
            "--sweep-args-b64",
            "Zm9v",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "pshare10_burst1.0_mc64.json" in result.output
    # The grid holds one engine point, so the sweep is driven over exactly it.
    assert [point.slug() for point in captured["points"]] == [_POINT.slug()]
    # All four collaborators reach the driver under their expected keywords, callable — so
    # a keyword-name mismatch or a dropped collaborator at the flow->driver seam fails.
    for name in ("deploy_fn", "scrape_fn", "point_sweep_fn", "has_pending_cells"):
        assert callable(captured[name]), name
    # The cell task is built on the run's bucket with the command's retries threaded, and
    # both boto3 clients on the run's region — so a dropped or hardcoded region is caught.
    assert captures["task_args"] == {"bucket": "bench-bucket", "retries": 2}
    assert captures["ssm_region"] == "us-east-1"
    assert captures["s3_region"] == "us-east-1"
    # The commercial arm and the extra load-cell flags reach the inputs the cells run under.
    assert built_inputs["inputs"].commercial is True
    assert built_inputs["inputs"].sweep_args_b64 == "Zm9v"
    # After the loop the terminal render task materialized the whole run off the flow-built
    # S3 client, under the run's prefix — so the flow->render wiring reaches S3 (ADR-0018).
    assert (
        "sweeps/run1/mns64_kvfp8_pcon/pshare10_burst1.0_mc64.json"
        in captures["s3"].keys
    )
    # ...and published the two tables as markdown artifacts to the parent run page, ceiling
    # first, after the config artifact the flow publishes before the loop — so a broken or
    # dropped publish at the real render task's seam fails, not just a broken materialize.
    assert [key for key, _ in captures["artifacts"]][-2:] == [
        "knob-sweep-ceiling-table",
        "knob-sweep-goodput-cliff",
    ]
    # ...and the two plots as image artifacts, each pointing at its public, virtual-hosted S3
    # URL in the run's region — so a dropped image publish or a mis-built URL at the render
    # seam fails (ADR-0018 Amendment).
    assert captures["images"] == [
        (
            "knob-sweep-ceiling-plot",
            (
                "https://bench-bucket.s3.us-east-1.amazonaws.com/"
                "sweeps/run1/charts/ceiling-by-max-num-seqs.png"
            ),
        ),
        (
            "knob-sweep-goodput-cliff-plot",
            (
                "https://bench-bucket.s3.us-east-1.amazonaws.com/"
                "sweeps/run1/charts/goodput-by-max-concurrency.png"
            ),
        ),
    ]
    # Each plot's PNG uploaded to S3 as its durable copy, under the run's charts prefix — so
    # the render's plot upload reached the flow-built client, the object the image URL points at.
    assert captures["s3"].uploads == [
        ("sweeps/run1/charts/ceiling-by-max-num-seqs.png", "image/png"),
        ("sweeps/run1/charts/goodput-by-max-concurrency.png", "image/png"),
    ]


def test_knob_sweep_rejects_a_results_dir_option(tmp_path: Path) -> None:
    # Each point's cell results download into an internal TemporaryDirectory, so the
    # command carries no --results-dir option an operator could point at a path.
    model_yaml, grid_yaml, manifest = _write_inputs(tmp_path)

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
        ],
    )

    assert result.exit_code != 0
    assert "No such option: --results-dir" in _plain(result.output)


def test_point_sweeps_nest_under_the_one_parent_knob_sweep_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _harness: None
) -> None:
    # A two-point grid, so the assertion is that *both* point sweeps observe the *same* one
    # parent run: a regression that spawned a fresh parent per point would still nest a
    # single point, but here it would report two distinct parent flow-run names.
    model_yaml, _grid_yaml, manifest = _write_inputs(tmp_path)
    grid_yaml = tmp_path / "sweep-grid.yaml"
    grid_yaml.write_text(_MULTI_GRID, encoding="utf-8")
    _stub_transports(monkeypatch)
    # A complete point skips its redeploy and scrape, so the real driver runs only the
    # point sweeps — isolating the lineage assertion from the fake kubectl's empty scrape.
    monkeypatch.setattr(knob_sweep_module, "point_is_complete", lambda *_a, **_k: True)
    seen: list[tuple[str | None, Any]] = []

    def _recording_point_sweep(**kwargs: Any) -> list[str]:
        from prefect.runtime import flow_run

        seen.append((flow_run.flow_name, flow_run.id))
        return [f"ptr:{kwargs['context'].point_slug}"]

    monkeypatch.setattr(knob_sweep_module, "run_point_sweep", _recording_point_sweep)

    pointers = knob_sweep_flow(
        run_id="run1",
        instance_id="i-1",
        region="us-east-1",
        bucket="bench-bucket",
        image_ref="repo:tag",
        model_yaml=model_yaml,
        sweep_grid=grid_yaml,
        vllm_manifest=manifest,
    )

    # Both point sweeps observe the one parent knob-sweep flow run — same name and same
    # run id — so a knob sweep's point sweeps share a parent in the Prefect UI (ADR-0015).
    assert [name for name, _id in seen] == ["knob-sweep", "knob-sweep"]
    assert len({run_id for _name, run_id in seen}) == 1
    assert len(pointers) == 2


def test_parent_knob_sweep_run_is_tagged_with_the_shared_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _harness: None
) -> None:
    # The parent knob-sweep flow run carries run=<run_id>, the same tag its point sweeps
    # group under, so the UI filters the whole sweep — parent and points — as one run.
    model_yaml, grid_yaml, manifest = _write_inputs(tmp_path)
    _stub_transports(monkeypatch)
    monkeypatch.setattr(knob_sweep_module, "point_is_complete", lambda *_a, **_k: True)
    captured: dict[str, Any] = {}

    def _recording_point_sweep(**_kwargs: Any) -> list[str]:
        from prefect.runtime import flow_run

        captured["parent_id"] = flow_run.id
        return ["ptr"]

    monkeypatch.setattr(knob_sweep_module, "run_point_sweep", _recording_point_sweep)

    knob_sweep_flow(
        run_id="run1",
        instance_id="i-1",
        region="us-east-1",
        bucket="bench-bucket",
        image_ref="repo:tag",
        model_yaml=model_yaml,
        sweep_grid=grid_yaml,
        vllm_manifest=manifest,
    )

    from prefect.client.orchestration import get_client

    with get_client(sync_client=True) as client:
        run = client.read_flow_run(captured["parent_id"])
    assert "run=run1" in run.tags


def test_register_knob_sweep_registers_a_module_path_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prefect.deployments.runner import EntrypointType

    captured: dict[str, Any] = {}

    def _fake_deploy(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "dep-123"

    monkeypatch.setattr(cli_module.knob_sweep_flow, "deploy", _fake_deploy)
    monkeypatch.setattr(cli_module, "_require_work_pool", lambda _wp: None)

    result = CliRunner().invoke(
        app,
        [
            "register-knob-sweep",
            "--region",
            "us-east-1",
            "--bucket",
            "bench-bucket",
            "--image-ref",
            "repo:tag",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "dep-123" in result.output
    # The worker runs the baked image, so the entrypoint is import-resolved (no source
    # fetch, no laptop path) and no image is built for the process pool.
    assert captured["name"] == "knob-sweep"
    assert captured["work_pool_name"] == "sweep-pool"
    assert captured["entrypoint_type"] is EntrypointType.MODULE_PATH
    assert captured["build"] is False
    assert captured["push"] is False
    # Every cluster-stable input defaults onto the deployment, keyed exactly as the flow's
    # parameters — a dropped or mis-named default is a run-time failure on the worker, since
    # `prefect deployment run` supplies only run_id and instance_id per run.
    assert captured["parameters"] == {
        "region": "us-east-1",
        "bucket": "bench-bucket",
        "image_ref": "repo:tag",
        "model_yaml": "/app/model.yaml",
        "sweep_grid": "/app/bench/sweep-grid.yaml",
        "vllm_manifest": "/app/k8s/vllm-gpu.yaml",
        "retries": 0,
        "commercial": False,
        "sweep_args_b64": "",
    }
    # The paths are serialized to str: Prefect runs parameters through serialize_parameters,
    # so a raw Path default would fail at real registration.
    for key in ("model_yaml", "sweep_grid", "vllm_manifest"):
        assert isinstance(captured["parameters"][key], str)


def test_register_knob_sweep_threads_overrides_onto_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_deploy(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "dep-456"

    monkeypatch.setattr(cli_module.knob_sweep_flow, "deploy", _fake_deploy)
    monkeypatch.setattr(cli_module, "_require_work_pool", lambda _wp: None)

    result = CliRunner().invoke(
        app,
        [
            "register-knob-sweep",
            "--region",
            "us-east-1",
            "--bucket",
            "bench-bucket",
            "--image-ref",
            "repo:tag",
            "--work-pool",
            "other-pool",
            "--model-yaml",
            "/opt/model.yaml",
            "--retries",
            "3",
            "--commercial",
            "--sweep-args-b64",
            "Zm9v",
        ],
    )

    assert result.exit_code == 0, result.output
    # An overridden option reaches deploy, not just the default — so an option defined but
    # not wired into flow.deploy is caught.
    assert captured["work_pool_name"] == "other-pool"
    assert captured["parameters"]["model_yaml"] == "/opt/model.yaml"
    assert captured["parameters"]["retries"] == 3
    assert captured["parameters"]["commercial"] is True
    assert captured["parameters"]["sweep_args_b64"] == "Zm9v"


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
@pytest.mark.parametrize("option", ["region", "bucket", "image-ref"])
def test_register_knob_sweep_rejects_an_empty_cluster_input(
    monkeypatch: pytest.MonkeyPatch, option: str, blank: str
) -> None:
    # An empty or whitespace-only region/bucket/image-ref would register a deployment whose
    # default surfaces as an obscure boto3 failure on the worker at run time; reject it at
    # registration instead. The whitespace case pins the `.strip()` guard: a bare falsiness
    # check would let "   " register a deployment defaulted to blanks.
    deployed = False

    def _fake_deploy(**_kwargs: Any) -> str:
        nonlocal deployed
        deployed = True
        return "dep-nope"

    monkeypatch.setattr(cli_module.knob_sweep_flow, "deploy", _fake_deploy)
    monkeypatch.setattr(cli_module, "_require_work_pool", lambda _wp: None)

    values = {"region": "us-east-1", "bucket": "bench-bucket", "image-ref": "repo:tag"}
    values[option] = blank
    argv = ["register-knob-sweep"]
    for name, value in values.items():
        argv += [f"--{name}", value]

    result = CliRunner().invoke(app, argv)

    # Exit 2 is typer's usage-error code (BadParameter), and deploy never fires — so the
    # empty input is rejected at registration, before any deployment is created. The
    # rendered message is not asserted: rich truncates it at the terminal width.
    assert result.exit_code == 2
    assert not deployed


def test_register_knob_sweep_surfaces_a_deploy_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A registration that reaches deploy and fails (API unreachable over the port-forward, a
    # Prefect-side rejection) must surface as a non-zero exit, not a swallowed success.
    def _failing_deploy(**_kwargs: Any) -> str:
        raise RuntimeError("prefect API unreachable")

    monkeypatch.setattr(cli_module.knob_sweep_flow, "deploy", _failing_deploy)
    monkeypatch.setattr(cli_module, "_require_work_pool", lambda _wp: None)

    result = CliRunner().invoke(
        app,
        [
            "register-knob-sweep",
            "--region",
            "us-east-1",
            "--bucket",
            "bench-bucket",
            "--image-ref",
            "repo:tag",
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


def test_register_knob_sweep_rejects_an_absent_work_pool(_harness: None) -> None:
    # `flow.deploy` only warns on a missing pool and still returns a deployment id, so a
    # deployment would register that no worker ever polls — every run sitting Scheduled
    # forever. The ephemeral server starts with no pools, so registration must fail fast
    # here rather than report success. deploy is left real: the pool pre-check runs first.
    result = CliRunner().invoke(
        app,
        [
            "register-knob-sweep",
            "--region",
            "us-east-1",
            "--bucket",
            "bench-bucket",
            "--image-ref",
            "repo:tag",
            "--work-pool",
            "ghost-pool",
        ],
    )

    assert result.exit_code == 2


def test_register_knob_sweep_creates_a_real_deployment(_harness: None) -> None:
    # Register against a real ephemeral server with the real deploy: catches a Prefect
    # API-contract break a faked deploy hides — a renamed kwarg, a dropped MODULE_PATH
    # entrypoint, or a parameter that fails serialize_parameters (a raw Path default).
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.actions import WorkPoolCreate

    with get_client(sync_client=True) as client:
        client.create_work_pool(WorkPoolCreate(name="sweep-pool", type="process"))

    result = CliRunner().invoke(
        app,
        [
            "register-knob-sweep",
            "--region",
            "us-east-1",
            "--bucket",
            "bench-bucket",
            "--image-ref",
            "repo:tag",
        ],
    )

    assert result.exit_code == 0, result.output
    with get_client(sync_client=True) as client:
        deployment = client.read_deployment_by_name("knob-sweep/knob-sweep")
    # The entrypoint is the dotted module path resolved by import, not a file path — so the
    # worker runs the baked image without fetching a source tree.
    assert deployment.entrypoint == (
        "slipstream_bench.orchestration.flows.knob_sweep.knob_sweep_flow"
    )
    assert deployment.work_pool_name == "sweep-pool"
    # The cluster-stable defaults round-trip through the server, paths serialized to str.
    assert deployment.parameters == {
        "region": "us-east-1",
        "bucket": "bench-bucket",
        "image_ref": "repo:tag",
        "model_yaml": "/app/model.yaml",
        "sweep_grid": "/app/bench/sweep-grid.yaml",
        "vllm_manifest": "/app/k8s/vllm-gpu.yaml",
        "retries": 0,
        "commercial": False,
        "sweep_args_b64": "",
    }
