"""The in-cluster collaborators: GPU redeploy, ceiling scrape, and GPU KV pool scrape.

Exercises deploy_gpu, deploy_gpu_point, scrape_ceiling, and scrape_gpu_kv_pool against
a fake kubectl that records the argv and stdin issued, and render/extract as pure
functions — no cluster. Asserts the redeploy applies only the Deployment document (the
worker's RBAC grants get/patch on deploy/vllm-gpu, not the manifest's Namespace or
Service), renders the point's knobs, then waits the rollout; and that each scrape returns
the last matching line or raises.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from slipstream_bench.contract import EnginePoint, SweepGrid
from slipstream_bench.orchestration.cluster import (
    CeilingScrapeError,
    KubectlError,
    KvPoolScrapeError,
    build_kubectl,
    deploy_gpu,
    deploy_gpu_point,
    extract_deployment_doc,
    render_gpu_manifest,
    scrape_ceiling,
    scrape_gpu_kv_pool,
)
from slipstream_bench.orchestration.grid import get_engine_args
from slipstream_bench.orchestration.kv_ab_config import (
    get_kv_ab_engine_args,
    load_kv_ab_config,
)

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

# A manifest with the three swept placeholders, the GPU KV pool cap, and the two KV cache
# offload ones, plus the Namespace and Service the operator applied at stack-up — the
# redeploy must apply only the Deployment.
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
            - --kv-cache-memory-bytes
            - "${KV_CACHE_MEMORY_BYTES}"
            - --max-num-seqs
            - "${MAX_NUM_SEQS}"
            - ${PREFIX_CACHING_FLAG}
            - --kv-transfer-config
            - '${KV_TRANSFER_CONFIG}'
          resources:
            requests:
              memory: ${MEMORY_LIMIT}
            limits:
              memory: ${MEMORY_LIMIT}
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-gpu
  namespace: slipstream
"""

_COMMITTED_MANIFEST = Path(__file__).resolve().parents[2] / "k8s" / "vllm-gpu.yaml"

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


def test_render_substitutes_the_swept_knobs() -> None:
    engine_args = get_engine_args(_grid(), _POINT)

    rendered = render_gpu_manifest(
        _MANIFEST, engine_args=engine_args, kv_cache_offload=False
    )

    assert "${" not in rendered
    assert '"64"' in rendered
    assert '"fp8"' in rendered
    assert "--enable-prefix-caching" in rendered


def _kv_cache_memory_bytes(container: dict[str, Any]) -> str:
    """The value the container passes to vLLM's --kv-cache-memory-bytes."""
    args = container["args"]
    return args[args.index("--kv-cache-memory-bytes") + 1]


def test_render_caps_the_gpu_kv_pool_at_the_engine_args_bytes() -> None:
    engine_args = replace(
        get_engine_args(_grid(), _POINT), kv_cache_memory_bytes=1_207_959_552
    )

    rendered = render_gpu_manifest(
        _MANIFEST, engine_args=engine_args, kv_cache_offload=False
    )

    assert _kv_cache_memory_bytes(_vllm_container(rendered)) == "1207959552"


def test_render_leaves_a_sweep_point_s_gpu_kv_pool_uncapped() -> None:
    rendered = render_gpu_manifest(
        _MANIFEST, engine_args=get_engine_args(_grid(), _POINT), kv_cache_offload=False
    )

    # vLLM parses the literal "None" as no cap: gpu-memory-utilization sizes the pool.
    assert _kv_cache_memory_bytes(_vllm_container(rendered)) == "None"


def test_render_maps_the_fp16_label_and_caching_off_flag() -> None:
    engine_args = get_engine_args(_grid(), _FP16_OFF)

    rendered = render_gpu_manifest(
        _MANIFEST, engine_args=engine_args, kv_cache_offload=False
    )

    # fp16 -> the engine token float16; the off arm's flag renders as the bare list item.
    assert '"float16"' in rendered
    assert "--no-enable-prefix-caching" in rendered
    assert '"128"' in rendered


@pytest.mark.parametrize(
    "placeholder",
    [
        "${MAX_NUM_SEQS}",
        "${KV_CACHE_DTYPE}",
        "${KV_CACHE_MEMORY_BYTES}",
        "${PREFIX_CACHING_FLAG}",
        "${KV_TRANSFER_CONFIG}",
        "${MEMORY_LIMIT}",
    ],
)
def test_render_rejects_a_manifest_missing_a_placeholder(placeholder: str) -> None:
    # A misspelled or dropped placeholder would otherwise render silently: the knob
    # never reaches vLLM, or a literal ${...} crashloops the pod after apply.
    manifest = _MANIFEST.replace(placeholder, "${MISSPELLED}")

    with pytest.raises(ValueError, match=re.escape(placeholder)):
        render_gpu_manifest(
            manifest,
            engine_args=get_engine_args(_grid(), _POINT),
            kv_cache_offload=False,
        )


def _vllm_container(rendered: str) -> dict[str, Any]:
    """The vllm container of the rendered manifest's Deployment."""
    deployment = yaml.safe_load(extract_deployment_doc(rendered))
    return deployment["spec"]["template"]["spec"]["containers"][0]


def _kv_transfer_config(container: dict[str, Any]) -> str:
    """The value the container passes to vLLM's --kv-transfer-config."""
    args = container["args"]
    return args[args.index("--kv-transfer-config") + 1]


def _render_committed_manifest(*, kv_cache_offload: bool) -> str:
    """Render the committed k8s/vllm-gpu.yaml for the rig point."""
    return render_gpu_manifest(
        _COMMITTED_MANIFEST.read_text(encoding="utf-8"),
        engine_args=get_engine_args(_grid(), _POINT),
        kv_cache_offload=kv_cache_offload,
    )


def test_render_with_kv_cache_offload_wires_the_lmcache_connector_and_memory() -> None:
    engine_args = get_engine_args(_grid(), _POINT)

    rendered = render_gpu_manifest(
        _MANIFEST, engine_args=engine_args, kv_cache_offload=True
    )

    container = _vllm_container(rendered)
    assert json.loads(_kv_transfer_config(container)) == {
        "kv_connector": "LMCacheConnectorV1",
        "kv_role": "kv_both",
    }
    assert container["resources"]["requests"]["memory"] == "12Gi"
    assert container["resources"]["limits"]["memory"] == "12Gi"


def test_render_without_kv_cache_offload_keeps_no_connector_and_the_default_memory() -> (
    None
):
    engine_args = get_engine_args(_grid(), _POINT)

    rendered = render_gpu_manifest(
        _MANIFEST, engine_args=engine_args, kv_cache_offload=False
    )

    # vLLM parses the literal "None" as no connector, its default.
    container = _vllm_container(rendered)
    assert _kv_transfer_config(container) == "None"
    assert "LMCacheConnectorV1" not in rendered
    assert container["resources"]["requests"]["memory"] == "8Gi"
    assert container["resources"]["limits"]["memory"] == "8Gi"


@pytest.mark.parametrize("kv_cache_offload", [False, True])
def test_the_committed_manifest_renders_every_placeholder(
    kv_cache_offload: bool,
) -> None:
    rendered = _render_committed_manifest(kv_cache_offload=kv_cache_offload)

    assert "${" not in rendered


def test_the_committed_manifest_renders_no_connector_with_offload_off() -> None:
    rendered = _render_committed_manifest(kv_cache_offload=False)

    container = _vllm_container(rendered)
    assert _kv_transfer_config(container) == "None"
    assert "LMCacheConnectorV1" not in rendered
    assert container["resources"]["limits"]["memory"] == "8Gi"


def test_the_committed_manifest_renders_the_connector_with_offload_on() -> None:
    container = _vllm_container(_render_committed_manifest(kv_cache_offload=True))

    assert json.loads(_kv_transfer_config(container)) == {
        "kv_connector": "LMCacheConnectorV1",
        "kv_role": "kv_both",
    }
    assert container["resources"]["limits"]["memory"] == "12Gi"


def _gibibytes(quantity: str) -> float:
    """A Kubernetes ``<n>Gi`` memory quantity in GiB."""
    assert quantity.endswith("Gi"), quantity
    return float(quantity.removesuffix("Gi"))


def test_the_committed_manifest_fits_lmcaches_cpu_tier_inside_the_offload_memory() -> (
    None
):
    off = _vllm_container(_render_committed_manifest(kv_cache_offload=False))
    on = _vllm_container(_render_committed_manifest(kv_cache_offload=True))

    env = {var["name"]: var.get("value") for var in on["env"]}
    assert env["LMCACHE_LOCAL_CPU"] == "True"
    assert env["LMCACHE_CHUNK_SIZE"] == "256"
    # The pinned tier sits on top of what the engine runs in with offload off.
    tier = float(env["LMCACHE_MAX_LOCAL_CPU_SIZE"])
    engine = _gibibytes(off["resources"]["limits"]["memory"])
    assert tier > 0
    assert engine + tier <= _gibibytes(on["resources"]["limits"]["memory"])


def test_the_recipe_renders_the_same_offload_values_as_the_worker() -> None:
    # `just _render-gpu-manifest` and render_gpu_manifest each spell the two states
    # out; an operator's gpu-deploy must match what the worker renders.
    recipe = (_COMMITTED_MANIFEST.parents[1] / "just" / "serve.just").read_text(
        encoding="utf-8"
    )
    off = _vllm_container(_render_committed_manifest(kv_cache_offload=False))
    on = _vllm_container(_render_committed_manifest(kv_cache_offload=True))

    assert f"kv_transfer_config={_kv_transfer_config(off)}\n" in recipe
    assert f"kv_transfer_config='{_kv_transfer_config(on)}'\n" in recipe
    assert f"memory_limit={off['resources']['limits']['memory']}\n" in recipe
    assert f"memory_limit={on['resources']['limits']['memory']}\n" in recipe


@pytest.mark.parametrize("workload", ["agent", "chat"])
def test_the_committed_manifest_caps_the_pool_at_the_kv_ab_workload_s_bytes(
    workload: str,
) -> None:
    config = load_kv_ab_config(
        _COMMITTED_MANIFEST.parents[1]
        / "bench"
        / f"kv-cache-extension-ab-{workload}.yaml"
    )

    rendered = render_gpu_manifest(
        _COMMITTED_MANIFEST.read_text(encoding="utf-8"),
        engine_args=get_kv_ab_engine_args(config.engine),
        kv_cache_offload=True,
    )

    # 16,384 tokens of fp8 KV at 73,728 bytes each.
    assert _kv_cache_memory_bytes(_vllm_container(rendered)) == "1207959552"


def test_the_recipe_substitutes_every_placeholder_in_the_committed_manifest() -> None:
    # envsubst leaves a placeholder off its allow-list as a literal ${...}, which parses
    # as YAML and then crashloops the pod a plain `just gpu-deploy` rolls out.
    recipe = (_COMMITTED_MANIFEST.parents[1] / "just" / "serve.just").read_text(
        encoding="utf-8"
    )
    allow_list = re.search(r"envsubst '([^']*)'", recipe)
    placeholders = set(
        re.findall(r"\$\{[A-Z_]+\}", _COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    )

    assert allow_list is not None
    assert placeholders == set(allow_list.group(1).split())


def test_the_recipe_leaves_the_gpu_kv_pool_uncapped_as_the_sweep_does() -> None:
    # A plain `just gpu-deploy` must size the pool as a sweep point's redeploy does.
    recipe = (_COMMITTED_MANIFEST.parents[1] / "just" / "serve.just").read_text(
        encoding="utf-8"
    )
    uncapped = _kv_cache_memory_bytes(
        _vllm_container(_render_committed_manifest(kv_cache_offload=False))
    )

    assert f'kv_cache_memory_bytes="${{KV_CACHE_MEMORY_BYTES:-{uncapped}}}"' in recipe


def test_get_engine_args_rejects_a_point_absent_from_the_grid() -> None:
    from slipstream_bench.contract import SweepGridError

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


def _applied_container(kubectl: _FakeKubectl) -> dict[str, Any]:
    """The vllm container of the Deployment the fake kubectl was asked to apply."""
    applied = kubectl.calls[0][1]
    assert applied is not None
    return _vllm_container(applied)


def test_deploy_renders_without_kv_cache_offload() -> None:
    kubectl = _FakeKubectl()

    deploy_gpu_point(_POINT, grid=_grid(), manifest_text=_MANIFEST, kubectl=kubectl)

    container = _applied_container(kubectl)
    assert _kv_transfer_config(container) == "None"
    assert container["resources"]["limits"]["memory"] == "8Gi"


def test_deploy_gpu_with_kv_cache_offload_applies_the_lmcache_connector() -> None:
    kubectl = _FakeKubectl()

    deploy_gpu(
        get_engine_args(_grid(), _POINT),
        kv_cache_offload=True,
        manifest_text=_MANIFEST,
        kubectl=kubectl,
    )

    container = _applied_container(kubectl)
    assert json.loads(_kv_transfer_config(container)) == {
        "kv_connector": "LMCacheConnectorV1",
        "kv_role": "kv_both",
    }
    assert container["resources"]["limits"]["memory"] == "12Gi"
    assert kubectl.calls[1][0][:2] == ["rollout", "status"]


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


_KV_POOL_LOG = """\
INFO startup begins
INFO GPU KV cache size: 9,999 tokens, Maximum concurrency for 4,096 tokens per request: 2.44x
INFO restarted
INFO GPU KV cache size: 185,696 tokens, Maximum concurrency for 4,096 tokens per request: 45.34x
INFO serving
"""


def test_scrape_gpu_kv_pool_reads_the_last_token_count_off_the_startup_log() -> None:
    kubectl = _FakeKubectl(logs=_KV_POOL_LOG)

    assert scrape_gpu_kv_pool(kubectl=kubectl) == 185_696
    assert kubectl.calls[0][0] == ["logs", "-n", "slipstream", "deployment/vllm-gpu"]


@pytest.mark.parametrize(
    "logs",
    [
        "INFO startup\nINFO serving, no pool line\n",
        "INFO GPU KV cache size: , tokens, Maximum ...\n",
    ],
)
def test_scrape_gpu_kv_pool_raises_when_the_log_carries_no_pool_size(
    logs: str,
) -> None:
    kubectl = _FakeKubectl(logs=logs)

    with pytest.raises(KvPoolScrapeError, match="deployment/vllm-gpu"):
        scrape_gpu_kv_pool(kubectl=kubectl)


def test_scrape_gpu_kv_pool_raises_on_an_empty_pool() -> None:
    kubectl = _FakeKubectl(logs="INFO GPU KV cache size: 0 tokens, Maximum ...\n")

    with pytest.raises(KvPoolScrapeError, match="empty"):
        scrape_gpu_kv_pool(kubectl=kubectl)


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
