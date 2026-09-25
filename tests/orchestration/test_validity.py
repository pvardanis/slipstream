"""A cell is *done* only when it is a real measurement, not merely a parseable file.

ADR-0012: the validity gate rejects not only a missing, half-written, or errored JSON
but a structurally-complete result from an unhealthy server — every request errored,
goodput 0 — which parses and carries all fields yet is not a measurement. The gate
reuses the ``aggregate-sweep`` parser for the structural predicate, then adds the
semantic sanity check (non-zero completed requests, error rate under threshold).
``validate_cell`` raises so the task never caches a failure; ``is_cell_valid`` is the
boolean skip predicate a re-run reads to decide whether a cell may be reused.
"""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from slipstream_bench.orchestration.validity import (
    InvalidCellError,
    is_cell_valid,
    validate_cell,
)

_HEALTHY = {
    "max_concurrency": 64,
    "prefix_share": 50,
    "num_prompts": 100,
    "completed": 98,
    "request_goodput": 90.0,
    "request_throughput": 95.0,
    "errors": [""] * 98 + ["Timeout"] * 2,
}


def _write(path: Path, record: Mapping[str, object]) -> Path:
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_a_healthy_cell_validates(tmp_path: Path) -> None:
    cell = _write(tmp_path / "cell.json", _HEALTHY)

    validate_cell(cell)  # does not raise
    assert is_cell_valid(cell) is True


def test_a_missing_file_is_not_valid(tmp_path: Path) -> None:
    absent = tmp_path / "absent.json"

    assert is_cell_valid(absent) is False
    with pytest.raises(InvalidCellError):
        validate_cell(absent)


def test_a_half_written_file_is_not_valid(tmp_path: Path) -> None:
    truncated = tmp_path / "cell.json"
    truncated.write_text('{"max_concurrency": 64, "prefix', encoding="utf-8")

    assert is_cell_valid(truncated) is False
    with pytest.raises(InvalidCellError):
        validate_cell(truncated)


def test_a_structurally_incomplete_cell_is_not_valid(tmp_path: Path) -> None:
    # Missing the closed-loop cap the aggregate-sweep parser requires.
    record = {key: value for key, value in _HEALTHY.items() if key != "max_concurrency"}
    cell = _write(tmp_path / "cell.json", record)

    assert is_cell_valid(cell) is False
    with pytest.raises(InvalidCellError):
        validate_cell(cell)


def test_a_degenerate_all_errored_cell_is_rejected(tmp_path: Path) -> None:
    # Parses and carries every field, but nothing completed and goodput is 0 — the
    # unhealthy-server result the ADR names. Not a measurement.
    degenerate = {
        **_HEALTHY,
        "completed": 0,
        "request_goodput": 0.0,
        "request_throughput": 0.0,
        "errors": ["Connection refused"] * 100,
    }
    cell = _write(tmp_path / "cell.json", degenerate)

    assert is_cell_valid(cell) is False
    with pytest.raises(InvalidCellError, match="completed"):
        validate_cell(cell)


def test_a_cell_over_the_error_threshold_is_rejected(tmp_path: Path) -> None:
    # A handful of requests slipped through an otherwise dead server: completed > 0,
    # but the error rate is far past the health threshold.
    mostly_errored = {
        **_HEALTHY,
        "completed": 3,
        "errors": [""] * 3 + ["Timeout"] * 97,
    }
    cell = _write(tmp_path / "cell.json", mostly_errored)

    assert is_cell_valid(cell) is False
    with pytest.raises(InvalidCellError, match="error rate"):
        validate_cell(cell)


def test_a_cell_that_attempted_nothing_is_rejected_not_a_crash(tmp_path: Path) -> None:
    # A malformed record: completed > 0 past a zero num_prompts. The error-rate
    # division must not blow up — the gate rejects it as not a measurement.
    no_attempts = {**_HEALTHY, "num_prompts": 0, "completed": 5}
    cell = _write(tmp_path / "cell.json", no_attempts)

    assert is_cell_valid(cell) is False
    with pytest.raises(InvalidCellError, match="attempted"):
        validate_cell(cell)


def test_a_cell_that_dropped_most_requests_is_rejected(tmp_path: Path) -> None:
    # completed far short of attempted, yet the errors array is empty — the dropped
    # requests never wrote an error string. The gate reads the count shortfall, not the
    # array, so a decimated broken cell cannot cache as a measurement with a zero rate.
    decimated = {**_HEALTHY, "num_prompts": 1000, "completed": 1, "errors": []}
    cell = _write(tmp_path / "cell.json", decimated)

    assert is_cell_valid(cell) is False
    with pytest.raises(InvalidCellError, match="error rate"):
        validate_cell(cell)


def test_the_error_rate_boundary_is_exclusive(tmp_path: Path) -> None:
    # Exactly at the 0.05 default passes (the check is `>`, not `>=`); one more failure
    # fails. Pins the boundary a refactor to `>=` would silently break.
    at = {**_HEALTHY, "num_prompts": 100, "completed": 95, "errors": [""] * 100}
    over = {**_HEALTHY, "num_prompts": 100, "completed": 94, "errors": [""] * 100}

    assert is_cell_valid(_write(tmp_path / "at.json", at)) is True
    assert is_cell_valid(_write(tmp_path / "over.json", over)) is False


def test_a_cell_completing_more_than_attempted_is_rejected(tmp_path: Path) -> None:
    # A malformed record: completed past attempted would make the shortfall negative
    # and slip a nonsense measurement through. Rejected as malformed.
    impossible = {**_HEALTHY, "num_prompts": 100, "completed": 101}
    cell = _write(tmp_path / "cell.json", impossible)

    assert is_cell_valid(cell) is False
    with pytest.raises(InvalidCellError, match="malformed"):
        validate_cell(cell)


def test_a_null_completed_is_rejected(tmp_path: Path) -> None:
    # completed present but null: not a whole-number count, so not a measurement.
    record = {**_HEALTHY, "completed": None}
    cell = _write(tmp_path / "cell.json", record)

    assert is_cell_valid(cell) is False
    with pytest.raises(InvalidCellError, match="completed"):
        validate_cell(cell)


def test_the_error_threshold_is_configurable(tmp_path: Path) -> None:
    # 3% errored: accepted under the 0.05 default, rejected under a strict 0.01 ceiling.
    noisy = {**_HEALTHY, "completed": 97, "errors": [""] * 97 + ["Timeout"] * 3}
    cell = _write(tmp_path / "cell.json", noisy)

    assert is_cell_valid(cell) is True
    assert is_cell_valid(cell, max_error_rate=0.01) is False
