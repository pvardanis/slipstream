"""The redeploy-skip gate: is an engine point's every cell already valid in S3?

Exercises point_is_complete against a fake S3 that serves valid, unhealthy, absent,
transiently-unreachable, or access-denied cell objects. Asserts a fully-valid point reads
complete (so the parent skips its deploy and scrape); an absent, transient, or degenerate
cell reads incomplete (so the point re-runs); and a failure re-running cannot fix — an
access denial — aborts the probe rather than degrading to a re-run.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError, EndpointConnectionError
from slipstream.contract import EnginePoint, SweepGrid

from slipstream_bench.orchestration.completion import point_is_complete


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "HeadObject")


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
    """Serve every key a valid result except the one whose key holds ``missing``.

    An absent object surfaces as a 404 ClientError — download_file heads it first.
    """

    def __init__(self, missing: str) -> None:
        super().__init__()
        self._missing = missing

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        if self._missing in key:
            self.keys.append(key)
            raise _client_error("404")
        super().download_file(_bucket, key, dest)


class _ForbiddenOneS3(_AllValidS3):
    """Deny the key holding ``forbidden`` with a 403 — a failure re-running cannot fix."""

    def __init__(self, forbidden: str) -> None:
        super().__init__()
        self._forbidden = forbidden

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        if self._forbidden in key:
            self.keys.append(key)
            raise _client_error("403")
        super().download_file(_bucket, key, dest)


class _UnreachableOneS3(_AllValidS3):
    """Fail the key holding ``unreachable`` with a transient endpoint error."""

    def __init__(self, unreachable: str) -> None:
        super().__init__()
        self._unreachable = unreachable

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        if self._unreachable in key:
            self.keys.append(key)
            raise EndpointConnectionError(endpoint_url="https://s3.amazonaws.com")
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


class _CorruptOneS3(_AllValidS3):
    """Download succeeds but writes unparseable bytes for the key holding ``corrupt``."""

    def __init__(self, corrupt: str) -> None:
        super().__init__()
        self._corrupt = corrupt

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        if self._corrupt in key:
            self.keys.append(key)
            Path(dest).write_text("not json {{{", encoding="utf-8")
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


def test_an_absent_cell_makes_the_point_incomplete() -> None:
    s3 = _MissingOneS3("pshare50_burst1.0_mc128.json")

    assert _probe(s3) is False


def test_an_access_denied_cell_aborts_the_probe() -> None:
    # A 403 is a failure re-running cannot fix, so the probe must not degrade it to a
    # re-run — it propagates and aborts the sweep at the first cell it denies.
    s3 = _ForbiddenOneS3("pshare10_burst1.0_mc64.json")

    with pytest.raises(ClientError):
        _probe(s3)


def test_a_transiently_unreachable_cell_makes_the_point_incomplete() -> None:
    # A network blip may clear, so the point re-runs rather than aborting the sweep.
    s3 = _UnreachableOneS3("pshare10_burst1.0_mc64.json")

    assert _probe(s3) is False


def test_a_degenerate_cell_makes_the_point_incomplete() -> None:
    s3 = _UnhealthyOneS3("pshare10_burst1.0_mc64.json")

    assert _probe(s3) is False


def test_a_corrupt_downloaded_cell_makes_the_point_incomplete() -> None:
    # The object exists and downloads, but its bytes do not parse: falseness comes from
    # the validity gate, not the download except — a separately documented pending cause.
    s3 = _CorruptOneS3("pshare10_burst1.0_mc64.json")

    assert _probe(s3) is False


def test_the_probe_stops_at_the_first_pending_cell() -> None:
    # The first enumerated cell is missing, so the probe returns before reading the rest.
    s3 = _MissingOneS3("pshare10_burst1.0_mc64.json")

    assert _probe(s3) is False
    assert len(s3.keys) == 1


def test_a_transient_read_failure_is_logged_with_its_key_and_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A transient failure is re-run, not swallowed: it logs so a persistent one is visible.
    s3 = _UnreachableOneS3("pshare10_burst1.0_mc64.json")

    with caplog.at_level(
        logging.WARNING, logger="slipstream_bench.orchestration.completion"
    ):
        assert _probe(s3) is False

    assert "could not be read" in caplog.text
    assert "pshare10_burst1.0_mc64.json" in caplog.text
    assert "EndpointConnectionError" in caplog.text
