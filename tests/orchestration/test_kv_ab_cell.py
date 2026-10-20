"""Tests for a kv-ab ladder rung's cell, its S3 addresses, and its config digest.

Each rung of a run is one cell of the workload's load knobs at the rung's concurrency.
The bench host copies the raw result under ``sweeps/<run-id>/``, so a run's cells run
under ``kv-ab/<run>/<arm>-r<n>`` and their raw results land apart from the knob sweep's,
while the labelled record lands under ``kv-ab/``. The digest names what a cached rung
was measured with, so a change to the engine or the cell re-measures it.
"""

import threading
from collections.abc import Iterator
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
    get_kv_ab_record_key,
    get_kv_ab_run_id,
)
from slipstream_bench.orchestration.kv_ab_config import load_kv_ab_config

_CONFIG = load_kv_ab_config(
    Path(__file__).parents[2] / "bench" / "kv-cache-extension-ab-agent.yaml"
)
_MODEL = "Qwen/Qwen3-8B-AWQ"
_SERVING = ServingConfig(model_yaml=b"model: {}\n", vllm_image_ref="vllm:v0.29.0")


def _digest(config=_CONFIG, concurrency: int = 4) -> str:
    return get_kv_ab_cell_digest(
        config,
        get_kv_ab_cell(config, concurrency, model=_MODEL),
        serving=_SERVING,
        image_ref="bench:sha",
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


def test_a_runs_raw_results_land_apart_from_the_knob_sweeps() -> None:
    cell = get_kv_ab_cell(_CONFIG, 4, model=_MODEL)

    run_id = get_kv_ab_run_id("ab1", "on-r2")

    assert get_cell_s3_key(run_id, cell) == (
        "sweeps/kv-ab/ab1/on-r2/pshare90_burst1.0_mc4.json"
    )


def test_a_rungs_record_lands_under_the_kv_ab_prefix() -> None:
    cell = get_kv_ab_cell(_CONFIG, 2, model=_MODEL)

    key = get_kv_ab_record_key("ab1", "off-r1", cell)

    assert key == "kv-ab/ab1/off-r1/pshare90_burst1.0_mc2.json"


def test_a_rungs_digest_changes_with_its_concurrency() -> None:
    assert _digest(concurrency=2) != _digest(concurrency=4)


def test_a_rungs_digest_changes_with_the_gpu_pool_cap() -> None:
    engine = _CONFIG.engine.model_copy(update={"kv_cache_memory_bytes": 2 * 1024**3})
    resized = _CONFIG.model_copy(update={"engine": engine})

    assert _digest(resized) != _digest()


def test_a_rungs_digest_is_stable_for_the_same_inputs() -> None:
    assert _digest() == _digest()


_EXPOSITION = "# TYPE vllm:prefix_cache_queries counter\n"


class _MetricsHandler(BaseHTTPRequestHandler):
    """Serve the exposition at /metrics and a 500 anywhere else."""

    def do_GET(self) -> None:
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
