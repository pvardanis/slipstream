"""Load a ``vllm bench serve`` result JSON file, the join point post-processors read.

The cost post-processor and the prefix-cache-hit scraper both join on the raw
client JSON the sweep saved. A missing file, unreadable bytes, malformed JSON,
or a non-object top level is an error here rather than a silent empty record
downstream.
"""

import json
import math
from pathlib import Path


class ResultError(Exception):
    """A result file that cannot be read as the JSON a post-processor joins on."""


def numeric_metric(
    record: dict, source: Path, name: str, *, error_cls: type[Exception]
) -> float:
    """Read one metric as a number, rejecting a missing, null, or non-numeric value.

    A metric present but null or a string would price as 0 in bare arithmetic,
    silently dropping that side of the cost, so the value's type is checked here
    rather than trusting the key to exist as a number. The post-processor supplies
    ``error_cls`` so the failure surfaces as that tool's own error.

    :param record: the parsed result record.
    :param source: the file the record came from, for the error message.
    :param name: the metric key to read.
    :param error_cls: the exception the caller raises for a bad metric.
    :return: the metric as a float.
    :raise error_cls: when the metric is absent, not a number, non-finite, or
        negative.
    """
    value = record.get(name)
    # bool is an int subclass but is never a valid token count or duration.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error_cls(f"result {source} missing or non-numeric metric {name}")
    # json.loads reads NaN/Infinity by default; they slip past a < 0 or <= 0 check
    # and price as a nonsense figure, so reject them before the arithmetic.
    if not math.isfinite(value):
        raise error_cls(f"result {source} has non-finite metric {name}")
    # A negative token count or duration is physically impossible and would invert
    # the split or the run cost.
    if value < 0:
        raise error_cls(f"result {source} has negative metric {name}")
    return float(value)


def read_result(path: Path) -> dict:
    """Read one result file into its parsed record.

    :param path: the result JSON file to read.
    :return: the parsed top-level JSON object.
    :raise ResultError: when the file is absent, unreadable, not JSON, or not a
        JSON object.
    """
    if not path.is_file():
        raise ResultError(f"result file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResultError(f"cannot read result {path}: {error}") from error
    if not isinstance(data, dict):
        raise ResultError(f"result {path} is not a JSON object")
    return data
