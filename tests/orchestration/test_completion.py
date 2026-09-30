"""The redeploy-skip gate: is an engine point's every cell already valid in S3?

Exercises point_is_complete against a fake S3 that serves valid, unhealthy, or missing
cell objects. Asserts a fully-valid point reads complete (so the parent skips its deploy
and scrape), while one missing or degenerate cell reads incomplete (so the point re-runs),
and that the probe checks every cell object under the point's run prefix.
"""

import json
from pathlib import Path
from typing import Any

import yaml

from slipstream_bench.orchestration.completion import point_is_complete
from slipstream_bench.sweep.aggregation import EnginePoint
from slipstream_bench.sweep.grid import SweepGrid

_GRID = """
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

_POINT = EnginePoint(max_num_seqs=64, kv_cache_dtype="fp8", prefix_caching=True)

_VALID = {
    "prefix_share": 10,
    "max_concurrency": 64,
    "num_prompts": 100,
    "completed": 99,
    "request_goodput": 90.0,
    "request_throughput": 95.0,
    "errors": [""] * 99 + ["Timeout"],
}
# 50 of 100 completed -> error rate 0.5, past the gate's 0.05 ceiling: parseable but
# not a measurement.
_UNHEALTHY = {**_VALID, "completed": 50}


class _AllValidS3:
    """Serve a valid result for every key, recording the keys read."""

    def __init__(self, record: dict[str, object] | None = None) -> None:
        self.keys: list[str] = []
        self._record = record if record is not None else _VALID

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        self.keys.append(key)
        Path(dest).write_text(json.dumps(self._record), encoding="utf-8")


class _MissingOneS3(_AllValidS3):
    """Serve every key a valid result except the one whose key holds ``missing``."""

    def __init__(self, missing: str) -> None:
        super().__init__()
        self._missing = missing

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        if self._missing in key:
            self.keys.append(key)
            raise FileNotFoundError(key)
        super().download_file(_bucket, key, dest)


class _UnhealthyOneS3(_AllValidS3):
    """Serve every key a valid result except the one whose key holds ``unhealthy``."""

    def __init__(self, unhealthy: str) -> None:
        super().__init__()
        self._unhealthy = unhealthy

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        if self._unhealthy in key:
            self.keys.append(key)
            Path(dest).write_text(json.dumps(_UNHEALTHY), encoding="utf-8")
            return
        super().download_file(_bucket, key, dest)


def _grid() -> SweepGrid:
    return SweepGrid.model_validate(yaml.safe_load(_GRID))


def _probe(s3: Any) -> bool:
    return point_is_complete(
        _POINT,
        grid=_grid(),
        run_prefix="run1",
        bucket="bench-bucket",
        s3_client=s3,
        model="Qwen/Qwen2.5-0.5B-Instruct",
    )


def test_a_point_whose_cells_are_all_valid_is_complete() -> None:
    s3 = _AllValidS3()

    assert _probe(s3) is True
    # 2 shares x 2 rungs = 4 cells, all under the point's run prefix.
    assert len(s3.keys) == 4
    assert all(key.startswith("sweeps/run1/mns64_kvfp8_pcon/") for key in s3.keys)


def test_a_missing_cell_makes_the_point_incomplete() -> None:
    s3 = _MissingOneS3("pshare50_burst1.0_mc128.json")

    assert _probe(s3) is False


def test_a_degenerate_cell_makes_the_point_incomplete() -> None:
    s3 = _UnhealthyOneS3("pshare10_burst1.0_mc64.json")

    assert _probe(s3) is False


def test_the_probe_stops_at_the_first_pending_cell() -> None:
    # The first enumerated cell is missing, so the probe returns before reading the rest.
    s3 = _MissingOneS3("pshare10_burst1.0_mc64.json")

    assert _probe(s3) is False
    assert len(s3.keys) == 1
