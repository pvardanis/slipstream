"""The knob sweep's in-cluster collaborators: GPU redeploy and ceiling scrape.

Exercises deploy_gpu_point and scrape_ceiling against a fake kubectl that records the
argv and stdin issued, and render/extract as pure functions — no cluster. Asserts the
redeploy applies only the Deployment document (the worker's RBAC grants get/patch on
deploy/vllm-gpu, not the manifest's Namespace or Service), renders the point's knobs,
then waits the rollout; and that the scrape returns the last ceiling line or raises.
"""

from collections.abc import Sequence

import pytest
import yaml
from slipstream.contract import EnginePoint, SweepGrid

from slipstream_bench.orchestration.cluster import (
    KubectlError,
    build_kubectl,
    deploy_gpu_point,
    extract_deployment_doc,
    render_gpu_manifest,
    scrape_ceiling,
)
from slipstream_bench.sweep.aggregation import CeilingScrapeError
from slipstream_bench.sweep.grid import get_engine_args

_GRID = """
tier1:
  max_num_seqs: [64, 128]
  kv_cache_dtype: [fp8, fp16]
  prefix_caching:
    "on":
      flag: --enable-prefix-caching
      prefix_share: [10, 50]
    "off":
      flag: --no-enable-prefix-caching
      prefix_share: [0]
tier2:
  max_concurrency: [64, 128]
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

# A manifest with the three swept placeholders plus the Namespace and Service the
# operator applied at stack-up — the redeploy must apply only the Deployment.
_MANIFEST = """\
apiVersion: v1
kind: Namespace
metadata:
  name: slipstream
---
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
          args:
            - --kv-cache-dtype
            - "${KV_CACHE_DTYPE}"
            - --max-num-seqs
            - "${MAX_NUM_SEQS}"
            - ${PREFIX_CACHING_FLAG}
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-gpu
  namespace: slipstream
"""

_POINT = EnginePoint(max_num_seqs=64, kv_cache_dtype="fp8", prefix_caching=True)
_FP16_OFF = EnginePoint(max_num_seqs=128, kv_cache_dtype="fp16", prefix_caching=False)


class _FakeKubectl:
    """Record every ``(argv, stdin)`` issued; serve canned logs to a ``logs`` call."""

    def __init__(self, logs: str = "") -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self._logs = logs

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append((list(args), stdin))
        return self._logs if "logs" in args else ""


def _grid() -> SweepGrid:
    return load_grid_from_text(_GRID)


def load_grid_from_text(text: str) -> SweepGrid:
    return SweepGrid.model_validate(yaml.safe_load(text))


def test_render_substitutes_the_three_swept_knobs() -> None:
    engine_args = get_engine_args(_grid(), _POINT)

    rendered = render_gpu_manifest(_MANIFEST, engine_args=engine_args)

    assert "${" not in rendered
    assert '"64"' in rendered
    assert '"fp8"' in rendered
    assert "--enable-prefix-caching" in rendered


def test_render_maps_the_fp16_label_and_caching_off_flag() -> None:
    engine_args = get_engine_args(_grid(), _FP16_OFF)

    rendered = render_gpu_manifest(_MANIFEST, engine_args=engine_args)

    # fp16 -> the engine token float16; the off arm's flag renders as the bare list item.
    assert '"float16"' in rendered
    assert "--no-enable-prefix-caching" in rendered
    assert '"128"' in rendered


def test_get_engine_args_rejects_a_point_absent_from_the_grid() -> None:
    from slipstream.contract import SweepGridError

    absent = EnginePoint(max_num_seqs=999, kv_cache_dtype="fp8", prefix_caching=True)
    with pytest.raises(SweepGridError):
        get_engine_args(_grid(), absent)


def test_extract_deployment_doc_returns_only_the_named_deployment() -> None:
    doc = extract_deployment_doc(_MANIFEST)

    parsed = list(yaml.safe_load_all(doc))
    assert len(parsed) == 1
    assert parsed[0]["kind"] == "Deployment"
    assert parsed[0]["metadata"]["name"] == "vllm-gpu"


def test_extract_deployment_doc_raises_when_no_deployment() -> None:
    only_service = "apiVersion: v1\nkind: Service\nmetadata:\n  name: vllm-gpu\n"
    with pytest.raises(ValueError, match="no Deployment named"):
        extract_deployment_doc(only_service)


def test_deploy_applies_only_the_deployment_then_waits_the_rollout() -> None:
    kubectl = _FakeKubectl()

    deploy_gpu_point(_POINT, grid=_grid(), manifest_text=_MANIFEST, kubectl=kubectl)

    assert len(kubectl.calls) == 2
    apply_args, apply_stdin = kubectl.calls[0]
    assert apply_args == ["apply", "-n", "slipstream", "-f", "-"]
    assert apply_stdin is not None
    applied = list(yaml.safe_load_all(apply_stdin))
    # Only the Deployment is applied: the worker's RBAC cannot touch the Namespace/Service.
    assert [doc["kind"] for doc in applied] == ["Deployment"]

    rollout_args, rollout_stdin = kubectl.calls[1]
    assert rollout_args == [
        "rollout",
        "status",
        "-n",
        "slipstream",
        "deployment/vllm-gpu",
        "--timeout=1200s",
    ]
    assert rollout_stdin is None


def test_deploy_renders_the_points_knobs_into_the_applied_deployment() -> None:
    kubectl = _FakeKubectl()

    deploy_gpu_point(_POINT, grid=_grid(), manifest_text=_MANIFEST, kubectl=kubectl)

    applied = kubectl.calls[0][1]
    assert applied is not None
    deployment_doc = next(iter(yaml.safe_load_all(applied)))
    args = deployment_doc["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "64" in args
    assert "fp8" in args
    assert "--enable-prefix-caching" in args
    assert "${MAX_NUM_SEQS}" not in args


def test_deploy_raises_when_kubectl_fails() -> None:
    def failing(_args: Sequence[str], _stdin: str | None = None) -> str:
        raise KubectlError("boom")

    with pytest.raises(KubectlError):
        deploy_gpu_point(_POINT, grid=_grid(), manifest_text=_MANIFEST, kubectl=failing)


_CEILING_LOG = """\
INFO startup begins
Maximum concurrency for 4,096 tokens per request: 10.30x
INFO more logs
Maximum concurrency for 4,096 tokens per request: 12.50x
INFO serving
"""


def test_scrape_returns_the_last_ceiling_line() -> None:
    kubectl = _FakeKubectl(logs=_CEILING_LOG)

    ceiling = scrape_ceiling(_POINT, kubectl=kubectl)

    assert ceiling == "Maximum concurrency for 4,096 tokens per request: 12.50x"
    log_args = kubectl.calls[0][0]
    assert log_args == ["logs", "-n", "slipstream", "deployment/vllm-gpu"]


def test_scrape_raises_when_the_log_carries_no_ceiling() -> None:
    kubectl = _FakeKubectl(logs="INFO startup\nINFO serving, no ceiling line\n")

    with pytest.raises(CeilingScrapeError, match=_POINT.slug()):
        scrape_ceiling(_POINT, kubectl=kubectl)


def test_build_kubectl_returns_stdout_and_passes_stdin() -> None:
    # `cat` echoes its stdin to stdout: proves the runner wires input -> output.
    run = build_kubectl(binary="cat")

    assert run([], "hello world") == "hello world"


def test_build_kubectl_raises_on_a_non_zero_exit() -> None:
    run = build_kubectl(binary="false")

    with pytest.raises(KubectlError, match="exited"):
        run([], None)


def test_build_kubectl_error_carries_the_argv_and_stderr() -> None:
    # An unattended sweep diagnoses a failed apply from the log alone, so the raised error
    # must carry both what ran and what the command wrote to stderr.
    run = build_kubectl(binary="sh")

    with pytest.raises(KubectlError) as excinfo:
        run(["-c", "echo boom >&2; exit 3"], None)

    message = str(excinfo.value)
    assert "boom" in message
    assert "exited 3" in message
    assert "-c" in message
