"""The validity gate: is a cell result a measurement, or only a parseable file?

ADR-0012 defines a cell as *done* only when its JSON exists, parses, carries the
expected fields, **and** passes a semantic sanity check — reusing the
``aggregate-sweep`` parser (:func:`slipstream_bench.sweep.aggregation.LoadCell.from_record`)
as the structural predicate rather than re-deriving field validation here. Structural
parseability alone is not trusted: a result from an unhealthy server — every request
errored, goodput 0 — parses and carries all fields yet is not a measurement, so the
gate also requires non-zero completed requests and an error rate under a health
threshold. :func:`validate_cell` raises so the sweep task never caches a failure;
:func:`is_cell_valid` is the boolean skip predicate a re-run reads.
"""

from pathlib import Path

from slipstream_bench.results import ResultError, read_result
from slipstream_bench.sweep.aggregation import LoadCell, SweepAggregationError

# The fraction of a cell's attempted requests allowed to fail before the result is
# read as an unhealthy server rather than a measured load point. vLLM's benchmark
# computes goodput and throughput over successful requests only, so a failed request
# is invisible to the goodput fraction — it merely shrinks the completed set. A server
# that drops requests but serves the survivors fast still shows a near-1.0 goodput
# fraction and would pass the ceiling test off a decimated set; the error rate is the
# only guard against that, so it must be strict. Closed-loop rungs under overload
# complete late (success, slow) rather than erroring, so a tight ceiling never rejects
# a real overload measurement — only a genuinely broken server trips it. Mirrors the
# 0.95 goodput floor the ceiling already reads off (ADR-0009): at most 5% may fail.
DEFAULT_MAX_ERROR_RATE = 0.05


class InvalidCellError(Exception):
    """A cell result that is not a usable measurement and must not be cached."""


def validate_cell(
    path: Path, *, max_error_rate: float = DEFAULT_MAX_ERROR_RATE
) -> None:
    """Assert a cell result is a real measurement, raising when it is not.

    Runs the structural predicate (the ``aggregate-sweep`` parser) then the semantic
    sanity check. The sweep task calls this so an invalid result raises out of the
    task and is never cached — the next run re-attempts the cell.

    :param path: the cell's ``vllm bench serve`` result JSON.
    :param max_error_rate: the fraction of attempted requests allowed to fail before
        the result is rejected as an unhealthy server.
    :raise InvalidCellError: when the file is missing, unreadable, unparseable,
        structurally incomplete, or a degenerate result (nothing completed, or an
        error rate past ``max_error_rate``).
    """
    try:
        record = read_result(path)
        LoadCell.from_record(record, path)
    except (ResultError, SweepAggregationError) as error:
        message = f"cell {path} is not a parseable result: {error}"
        raise InvalidCellError(message) from error
    completed = _require_count(record, path, "completed")
    attempted = _require_count(record, path, "num_prompts")
    _require_measurement(completed, attempted, path, max_error_rate)


def is_cell_valid(
    path: Path, *, max_error_rate: float = DEFAULT_MAX_ERROR_RATE
) -> bool:
    """Report whether a cell result is a real measurement, never raising.

    The skip predicate a re-run reads: a cell may be reused only when this is true,
    so a missing, half-written, or degenerate result re-runs rather than freezing
    into the campaign.

    :param path: the cell's result JSON.
    :param max_error_rate: the health threshold passed to :func:`validate_cell`.
    :return: True when the cell passes the gate, False on any validation failure.
    """
    try:
        validate_cell(path, max_error_rate=max_error_rate)
    except InvalidCellError:
        return False
    return True


def _require_measurement(
    completed: int, attempted: int, path: Path, max_error_rate: float
) -> None:
    """Reject a parseable-but-degenerate cell: nothing completed, or errors dominate.

    The error rate is the count identity ``(attempted - completed) / attempted``, not
    a tally of the ``errors`` array. Every dispatched request either completes or fails
    with no retry (vLLM's benchmark), so a request that vanished — dropped by a broken
    server, never written as an error string — still lowers ``completed`` and so is
    counted as a failure here. Reading the array instead would let a decimated cell
    with an empty ``errors`` list pass with a zero rate.

    :param completed: the count of completed requests the cell reported.
    :param attempted: the count of requests the cell offered (``num_prompts``).
    :param path: the cell's result file, for the error message.
    :param max_error_rate: the tolerated failed fraction.
    :raise InvalidCellError: when the cell attempted nothing, no request completed,
        or the error rate exceeds the threshold.
    """
    if attempted <= 0:
        raise InvalidCellError(
            f"cell {path} attempted no requests (num_prompts {attempted}): "
            f"not a measurement"
        )
    if completed <= 0:
        raise InvalidCellError(
            f"cell {path} completed no requests: an unhealthy-server result, not a "
            f"measurement"
        )
    if completed > attempted:
        raise InvalidCellError(
            f"cell {path} completed {completed} of {attempted} attempted: "
            f"a malformed result, not a measurement"
        )
    failures = attempted - completed
    error_rate = failures / attempted
    if error_rate > max_error_rate:
        raise InvalidCellError(
            f"cell {path} error rate {error_rate:.2f} exceeds {max_error_rate:.2f} "
            f"({failures} of {attempted} requests failed): an unhealthy-server "
            f"result, not a measurement"
        )


def _require_count(record: dict[str, object], path: Path, key: str) -> int:
    """Read a whole-number request count the gate needs, rejecting a bad value.

    :param record: the parsed cell record.
    :param path: the cell's result file, for the error message.
    :param key: the count field to read.
    :return: the field as an int.
    :raise InvalidCellError: when the field is absent, null, or not a whole number
        — bool is an int subclass but is never a valid count.
    """
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCellError(f"cell {path} missing or non-integer {key}")
    return value
