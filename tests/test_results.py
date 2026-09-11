"""Tests for the shared result-JSON reader every post-processor joins on.

Pin the fail-fast contract: a missing file, unreadable bytes, malformed JSON,
or a non-object top level is an error, not a silent empty record.
"""

import json
from pathlib import Path

import pytest

from slipstream_bench.results import ResultError, read_result


def test_reads_a_result_object(tmp_path: Path) -> None:
    """A well-formed result file is parsed into its dict."""
    result = tmp_path / "cell.json"
    result.write_text(json.dumps({"model_id": "m", "duration": 3600.0}))

    assert read_result(result) == {"model_id": "m", "duration": 3600.0}


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    """A path with no file fails fast naming the file."""
    with pytest.raises(ResultError, match="not found"):
        read_result(tmp_path / "nope.json")


def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    """Bytes that are not JSON fail fast rather than surfacing as an empty read."""
    result = tmp_path / "bad.json"
    result.write_text("{not json")

    with pytest.raises(ResultError, match="cannot read"):
        read_result(result)


def test_non_object_top_level_is_rejected(tmp_path: Path) -> None:
    """A JSON array or scalar is not a result record the post-processors join on."""
    result = tmp_path / "array.json"
    result.write_text("[1, 2, 3]")

    with pytest.raises(ResultError, match="not a JSON object"):
        read_result(result)
