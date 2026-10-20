"""The kv-ab driver and flow over their production collaborators, with the transports faked.

:func:`build_kv_ab_collaborators` binds the redeploy, the GPU KV pool scrape, the warmup,
the ladder rungs, and the pending-run gate to kubectl, SSM, S3, and the engine's /metrics.
These tests drive :func:`drive_kv_ab` over them, with real Prefect tasks whose storage is a
tmp local filesystem, and assert what each arm run deploys, runs, and records. The flow is
run once with its transport builders swapped for the same fakes.
"""

import base64
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prefect import Task, flow
from prefect.filesystems import LocalFileSystem
from prefect.testing.utilities import prefect_test_harness

from slipstream_bench.orchestration.digest import ServingConfig
from slipstream_bench.orchestration.flows import kv_ab as kv_ab_module
from slipstream_bench.orchestration.flows.kv_ab import (
    KvAbInputs,
    KvAbResult,
    build_kv_ab_collaborators,
    drive_kv_ab,
    kv_ab_flow,
)
from slipstream_bench.orchestration.kv_ab_config import load_kv_ab_config
from slipstream_bench.orchestration.kv_ab_rig import KvAbRig
from slipstream_bench.orchestration.tasks.kv_ab_cell import build_kv_ab_cell_task
from slipstream_bench.orchestration.validity import InvalidCellError

_REPO = Path(__file__).parents[2]
# One run per arm keeps the script short: off-r1, then on-r1.
_CONFIG = load_kv_ab_config(
    _REPO / "bench" / "kv-cache-extension-ab-agent.yaml"
).model_copy(update={"runs_per_arm": 1})
_RIG = KvAbRig(max_model_len=4096, cpu_tier_bytes=4 * 1024**3, chunk_tokens=256)
_MODEL = "Qwen/Qwen3-8B-AWQ"
_CELL_SCRIPT = "/usr/local/bin/bench-sweep.sh"
_PROXY_SCRIPT = "/usr/local/bin/bench-proxy-up.sh"
_CONNECTOR = "LMCacheConnectorV1"
_RUN_ID = re.compile(r"RUN_ID='([^']+)'")
_CELL_CONFIG = re.compile(r"CELL_CONFIG_B64='([^']+)'")

_INPUTS = KvAbInputs(
    config=_CONFIG,
    manifest_text=(_REPO / "k8s" / "vllm-gpu.yaml").read_text(encoding="utf-8"),
    rig=_RIG,
    run="ab1",
    serving=ServingConfig(model_yaml=b"model: {}\n", vllm_image_ref="vllm:v0.29.0"),
    instance_id="i-1",
    image_ref="bench:sha",
    bucket="bench",
    model=_MODEL,
)

_VALID_RESULT = {
    "prefix_share": 90,
    "max_concurrency": 4,
    "num_prompts": 128,
    "completed": 128,
    "request_goodput": 2.0,
    "request_throughput": 2.0,
    "p95_ttft_ms": 850.0,
    "p95_tpot_ms": 42.0,
    "output_throughput": 256.0,
    "errors": [""] * 128,
}

# Every request refused: a cell that ran but measured nothing.
_FAILED_RESULT = {
    **_VALID_RESULT,
    "completed": 0,
    "errors": ["Connection refused"] * 128,
}


@pytest.fixture(scope="module", autouse=True)
def _harness() -> Iterator[None]:
    with prefect_test_harness():
        yield


class _Kubectl:
    """Record each applied Deployment; report the configured pool in the startup log."""

    def __init__(self) -> None:
        self.applied: list[str] = []

    def __call__(self, args: Sequence[str], stdin: str | None) -> str:
        if args[0] == "apply":
            assert stdin is not None
            self.applied.append(stdin)
        if args[0] == "logs":
            return "INFO GPU KV cache size: 16,384 tokens\n"
        return ""


class _Ssm:
    """Accept every command, recording the bench host's script runs in order."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def send_command(self, **kwargs: Any) -> dict[str, object]:
        self.commands.append(kwargs["Parameters"]["commands"][0])
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        return {"Status": "Success", "StandardErrorContent": ""}

    def script_runs(self) -> list[str]:
        """Name each run: ``proxy``, or a cell's ``<RUN_ID>@mc<concurrency>``."""
        runs = []
        for command in self.commands:
            if _PROXY_SCRIPT in command:
                runs.append("proxy")
                continue
            assert _CELL_SCRIPT in command, command
            run_id = _RUN_ID.search(command)
            config = _CELL_CONFIG.search(command)
            assert run_id and config, command
            cap = re.search(
                r"max_concurrency: (\d+)", base64.b64decode(config[1]).decode()
            )
            assert cap, command
            runs.append(f"{run_id[1]}@mc{cap[1]}")
        return runs


class _S3:
    """Serve each cell a valid result, or a failed one under a key holding a marker.

    Records each object written.
    """

    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.puts: dict[str, dict[str, Any]] = {}
        self._failing = failing

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        failed = any(marker in key for marker in self._failing)
        result = _FAILED_RESULT if failed else _VALID_RESULT
        Path(dest).write_text(json.dumps(result), encoding="utf-8")

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> None:
        assert (Bucket, ContentType) == ("bench", "application/json")
        self.puts[Key] = json.loads(Body)


class _Metrics:
    """Advance the prefix-cache counters by one cell's traffic at each after-cell read.

    The counters start from a warm engine's totals, so a cell's window (600/1,000 GPU
    hits) differs from the lifetime ratio. The connector's counters advance only while
    the last applied Deployment loads LMCache, as vLLM's do.
    """

    def __init__(self, kubectl: _Kubectl) -> None:
        self._kubectl = kubectl
        self._reads = 0
        self._counters = {
            "vllm:prefix_cache_queries": 50_000,
            "vllm:prefix_cache_hits": 10_000,
            "vllm:external_prefix_cache_queries": 0,
            "vllm:external_prefix_cache_hits": 0,
        }

    def __call__(self) -> str:
        self._reads += 1
        if self._reads % 2 == 0:
            self._advance_one_cell()
        lines = []
        for metric, value in self._counters.items():
            lines.append(f"# TYPE {metric} counter")
            lines.append(f'{metric}_total{{model_name="{_MODEL}"}} {value}')
        return "\n".join(lines) + "\n"

    def _advance_one_cell(self) -> None:
        self._counters["vllm:prefix_cache_queries"] += 1000
        self._counters["vllm:prefix_cache_hits"] += 600
        if _CONNECTOR in self._kubectl.applied[-1]:
            self._counters["vllm:external_prefix_cache_queries"] += 400
            self._counters["vllm:external_prefix_cache_hits"] += 300


class _Ab:
    """One A/B's fakes and production collaborators, over a shared cell task."""

    def __init__(self, task: Task[..., str], failing: tuple[str, ...] = ()) -> None:
        self.kubectl = _Kubectl()
        self.ssm = _Ssm()
        self.s3 = _S3(failing)
        self.collaborators = build_kv_ab_collaborators(
            _INPUTS,
            kubectl=self.kubectl,
            ssm_client=self.ssm,
            s3_client=self.s3,
            task=task,
            read_metrics=_Metrics(self.kubectl),
        )

    def drive(self) -> KvAbResult:
        """Drive the A/B inside a flow run, as the deployed flow does."""

        @flow
        def _ab() -> KvAbResult:
            return drive_kv_ab(_CONFIG, _RIG, self.collaborators)

        return _ab()


def _isolated_task(tmp_path: Path) -> Task[..., str]:
    results = LocalFileSystem(basepath=str(tmp_path / "results"))
    results.save(f"results-{uuid4().hex}", overwrite=True)
    keys = LocalFileSystem(basepath=str(tmp_path / "cache-keys"))
    keys.save(f"cache-keys-{uuid4().hex}", overwrite=True)
    return build_kv_ab_cell_task(result_storage=results, key_storage=keys)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run", " "),
        ("instance_id", ""),
        ("image_ref", " "),
        ("bucket", ""),
        ("model", " "),
        ("proxy_timeout_s", 0.0),
        ("cell_timeout_s", -1.0),
    ],
)
def test_inputs_that_cannot_address_or_bound_a_cell_are_rejected(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=f"KvAbInputs.{field}"):
        replace(_INPUTS, **{field: value})


def test_each_arm_run_redeploys_with_its_offload_switch(tmp_path: Path) -> None:
    ab = _Ab(_isolated_task(tmp_path))

    ab.drive()

    off, on = ab.kubectl.applied
    assert _CONNECTOR not in off
    assert _CONNECTOR in on
    # Both arms pin the GPU pool to the workload's cap, so only the offload differs.
    cap = str(_CONFIG.engine.kv_cache_memory_bytes)
    assert all(cap in doc for doc in (off, on))


def test_each_run_starts_the_proxy_warms_up_unrecorded_then_runs_its_ladder(
    tmp_path: Path,
) -> None:
    ab = _Ab(_isolated_task(tmp_path))

    ab.drive()

    assert ab.ssm.script_runs() == [
        "proxy",
        "kv-ab/ab1/off-r1/warmup@mc1",
        "kv-ab/ab1/off-r1@mc1",
        "kv-ab/ab1/off-r1@mc2",
        "kv-ab/ab1/off-r1@mc4",
        "proxy",
        "kv-ab/ab1/on-r1/warmup@mc1",
        "kv-ab/ab1/on-r1@mc1",
        "kv-ab/ab1/on-r1@mc2",
        "kv-ab/ab1/on-r1@mc4",
    ]
    assert not any("warmup" in key for key in ab.s3.puts)


def test_every_rung_records_its_labelled_window_under_kv_ab(tmp_path: Path) -> None:
    ab = _Ab(_isolated_task(tmp_path))

    result = ab.drive()

    keys = [
        f"kv-ab/ab1/{run}/pshare90_burst1.0_mc{cap}.json"
        for run in ("off-r1", "on-r1")
        for cap in (1, 2, 4)
    ]
    assert list(ab.s3.puts) == keys
    assert result.pointers == tuple(f"s3://bench/{key}" for key in keys)
    labels = ab.s3.puts["kv-ab/ab1/on-r1/pshare90_burst1.0_mc4.json"]["kv_ab"]
    # The cell's own 600/1,000, not the engine's lifetime ratio.
    assert (labels["arm"], labels["run"], labels["hit_rate"]) == ("on", 1, 0.6)
    assert labels["combined_hit_rate"] == 0.9


def test_a_completed_ab_resumes_every_run_without_redeploying(tmp_path: Path) -> None:
    task = _isolated_task(tmp_path)
    first = _Ab(task).drive()
    resumed = _Ab(task)

    result = resumed.drive()

    assert (result.runs_run, result.runs_resumed) == (0, 2)
    assert result.pointers == first.pointers
    assert resumed.kubectl.applied == []
    assert resumed.ssm.commands == []


def test_a_failed_warmup_stops_the_run_before_its_ladder(tmp_path: Path) -> None:
    ab = _Ab(_isolated_task(tmp_path), failing=("/warmup/",))

    with pytest.raises(InvalidCellError):
        ab.drive()

    assert ab.ssm.script_runs() == ["proxy", "kv-ab/ab1/off-r1/warmup@mc1"]
    assert ab.s3.puts == {}


def test_a_partly_cached_run_is_redeployed_and_re_measured_whole(
    tmp_path: Path,
) -> None:
    task = _isolated_task(tmp_path)
    # off-r1's mc1 rung is measured and cached; its mc2 rung fails, failing the A/B.
    with pytest.raises(InvalidCellError):
        _Ab(task, failing=("off-r1/pshare90_burst1.0_mc2",)).drive()
    resumed = _Ab(task)

    result = resumed.drive()

    assert (result.runs_run, result.runs_resumed) == (2, 0)
    # The redeploy cleared the cached rung's tiers, so it is measured again on this deploy.
    assert resumed.ssm.script_runs()[:5] == [
        "proxy",
        "kv-ab/ab1/off-r1/warmup@mc1",
        "kv-ab/ab1/off-r1@mc1",
        "kv-ab/ab1/off-r1@mc2",
        "kv-ab/ab1/off-r1@mc4",
    ]
    assert "kv-ab/ab1/off-r1/pshare90_burst1.0_mc1.json" in resumed.s3.puts


def test_the_flow_drives_its_workload_and_returns_the_record_pointers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload = tmp_path / "kv-ab.yaml"
    agent = (_REPO / "bench" / "kv-cache-extension-ab-agent.yaml").read_text(
        encoding="utf-8"
    )
    workload.write_text(agent.replace("runs_per_arm: 3", "runs_per_arm: 1"))
    task = _isolated_task(tmp_path)
    kubectl = _Kubectl()
    metrics = _Metrics(kubectl)
    metrics_urls: list[str] = []

    def _fetch(url: str) -> str:
        metrics_urls.append(url)
        return metrics()

    monkeypatch.setattr(kv_ab_module, "build_kubectl", lambda: kubectl)
    monkeypatch.setattr(kv_ab_module, "build_ssm_client", lambda _region: _Ssm())
    monkeypatch.setattr(kv_ab_module, "build_s3_client", lambda _region: _S3())
    monkeypatch.setattr(kv_ab_module, "kv_ab_cell_task", lambda _bucket, **_k: task)
    monkeypatch.setattr(kv_ab_module, "fetch_metrics", _fetch)

    pointers = kv_ab_flow(
        run_id="ab1",
        instance_id="i-1",
        region="us-east-1",
        bucket="bench",
        image_ref="bench:sha",
        model_yaml=_REPO / "model.yaml",
        kv_ab_config=workload,
        vllm_manifest=_REPO / "k8s" / "vllm-gpu.yaml",
        metrics_url="http://engine:8000/metrics",
    )

    assert pointers == [
        f"s3://bench/kv-ab/ab1/{run}/pshare90_burst1.0_mc{cap}.json"
        for run in ("off-r1", "on-r1")
        for cap in (1, 2, 4)
    ]
    assert set(metrics_urls) == {"http://engine:8000/metrics"}
