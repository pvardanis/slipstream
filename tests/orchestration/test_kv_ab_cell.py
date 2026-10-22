"""Tests for a kv-ab ladder rung's cell, its S3 addresses, and its config digest.

Each rung of a run is one cell of the workload's load knobs at the rung's concurrency.
The bench host copies the raw result under ``<run-id>/``, so a run's cells run under
``kv-offload-ab/<run>/<arm>-r<n>`` and their raw results land apart from the knob sweep's,
with each rung's labels beside its raw result. The digest names what a cached rung was
measured with, so a change to the engine or the cell re-measures it.
"""

import threading
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from slipstream_bench.orchestration.cell_run import get_cell_knobs, get_cell_s3_key
from slipstream_bench.orchestration.digest import ServingConfig
from slipstream_bench.orchestration.kv_ab_cell import (
    MetricsError,
    fetch_metrics,
    get_kv_ab_cell,
    get_kv_ab_cell_digest,
    get_kv_ab_label_key,
    get_kv_ab_result_key,
    get_kv_ab_run_id,
)
from slipstream_bench.orchestration.kv_ab_config import load_kv_ab_config
from slipstream_bench.orchestration.kv_ab_rig import KvAbRig

_CONFIG = load_kv_ab_config(
    Path(__file__).parents[2] / "bench" / "configs" / "kv-cache-extension-ab.agent.yaml"
)
_MODEL = "Qwen/Qwen3-8B-AWQ"
_SERVING = ServingConfig(model_yaml=b"model: {}\n", vllm_image_ref="vllm:v0.29.0")
_RIG = KvAbRig(max_model_len=4096, cpu_tier_bytes=4 * 1024**3, chunk_tokens=256)


def _digest(config=_CONFIG, concurrency: int = 4, rig: KvAbRig = _RIG) -> str:
    return get_kv_ab_cell_digest(
        config,
        get_kv_ab_cell(config, concurrency, model=_MODEL),
        serving=_SERVING,
        image_ref="bench:sha",
        rig=rig,
    )


def test_a_rung_cell_runs_the_workload_load_at_its_concurrency() -> None:
    cell = get_kv_ab_cell(_CONFIG, 4, model=_MODEL)

    assert get_cell_knobs(cell) == {
        "total_len": 3584,
        "num_prompts": 128,
        "num_prefixes": 8,
        "output_len": 128,
        "align_blocks": 256,
        "request_rate": "inf",
        "seed": 0,
        "goodput": ["ttft:1000", "tpot:50"],
        "tokenizer": None,
        "prefix_share": 90,
        "burstiness": 1.0,
        "max_concurrency": 4,
    }
    assert cell.model == _MODEL


def test_a_runs_raw_results_land_under_the_kv_offload_ab_prefix() -> None:
    cell = get_kv_ab_cell(_CONFIG, 4, model=_MODEL)

    run_id = get_kv_ab_run_id("ab1", "on-r2")

    assert get_cell_s3_key(run_id, cell) == (
        "kv-offload-ab/ab1/on-r2/pshare90_burst1.0_mc4.json"
    )


def test_a_rungs_labels_land_beside_its_raw_result() -> None:
    result_key = "kv-offload-ab/ab1/off-r1/pshare90_burst1.0_mc2.json"

    assert get_kv_ab_label_key(result_key) == (
        "kv-offload-ab/ab1/off-r1/pshare90_burst1.0_mc2.kv_ab.json"
    )


def test_a_rungs_raw_result_is_addressed_from_its_labels() -> None:
    result_key = "kv-offload-ab/ab1/off-r1/pshare90_burst1.0_mc2.json"

    assert get_kv_ab_result_key(get_kv_ab_label_key(result_key)) == result_key


def test_a_result_key_that_is_not_json_has_no_labels_key() -> None:
    with pytest.raises(ValueError, match="not a .json"):
        get_kv_ab_label_key("kv-offload-ab/ab1/off-r1/pshare90_burst1.0_mc2")


def test_a_key_that_is_not_a_labels_key_has_no_result_key() -> None:
    with pytest.raises(ValueError, match="not a .kv_ab.json"):
        get_kv_ab_result_key("kv-offload-ab/ab1/off-r1/pshare90_burst1.0_mc2.json")


def test_a_rungs_digest_changes_with_its_concurrency() -> None:
    assert _digest(concurrency=2) != _digest(concurrency=4)


def test_a_rungs_digest_changes_with_the_gpu_pool_cap() -> None:
    engine = _CONFIG.engine.model_copy(update={"kv_cache_memory_bytes": 2 * 1024**3})
    resized = _CONFIG.model_copy(update={"engine": engine})

    assert _digest(resized) != _digest()


@pytest.mark.parametrize(
    "rig",
    [
        replace(_RIG, cpu_tier_bytes=8 * 1024**3),
        replace(_RIG, chunk_tokens=512),
        replace(_RIG, max_model_len=8192),
    ],
)
def test_a_rungs_digest_changes_with_the_rig(rig: KvAbRig) -> None:
    # A resized CPU tier re-measures the on arm rather than serving the old tier's cells.
    assert _digest(rig=rig) != _digest()


def test_a_rungs_digest_is_stable_for_the_same_inputs() -> None:
    assert _digest() == _digest()


_EXPOSITION = "# TYPE vllm:prefix_cache_queries counter\n"


class _MetricsHandler(BaseHTTPRequestHandler):
    """Serve the exposition at /metrics, a broken body at two paths, a 500 elsewhere."""

    def do_GET(self) -> None:
        if self.path == "/truncated":
            # Promise more bytes than are sent, then close mid-body.
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"vllm:prefix")
            return
        if self.path == "/binary":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"\xff\xfe")
            return
        status, body = (200, _EXPOSITION) if self.path == "/metrics" else (500, "")
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output clean of the server's access log."""


@pytest.fixture
def _engine() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_the_metrics_fetch_reads_the_engines_exposition(_engine: str) -> None:
    assert fetch_metrics(f"{_engine}/metrics") == _EXPOSITION


def test_an_engine_answering_with_an_error_fails_the_fetch(_engine: str) -> None:
    with pytest.raises(MetricsError, match="500"):
        fetch_metrics(f"{_engine}/broken")


def test_an_unreachable_engine_fails_the_fetch() -> None:
    # Port 9 (discard) on loopback has no listener, so the connection is refused.
    with pytest.raises(MetricsError, match="127.0.0.1:9"):
        fetch_metrics("http://127.0.0.1:9/metrics", timeout_s=1.0)


@pytest.mark.parametrize("path", ["/truncated", "/binary"])
def test_an_engine_whose_body_cannot_be_read_fails_the_fetch(
    _engine: str, path: str
) -> None:
    with pytest.raises(MetricsError, match=path):
        fetch_metrics(f"{_engine}{path}")


def test_a_metrics_url_that_is_not_http_is_rejected(tmp_path: Path) -> None:
    exposition = tmp_path / "metrics"
    exposition.write_text(_EXPOSITION, encoding="utf-8")

    with pytest.raises(MetricsError, match="http"):
        fetch_metrics(exposition.as_uri())
