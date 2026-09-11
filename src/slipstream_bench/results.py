"""Load a ``vllm bench serve`` result JSON file, the join point post-processors read.

The cost post-processor and the prefix-cache-hit scraper both join on the raw
client JSON the sweep saved. A missing file, unreadable bytes, malformed JSON,
or a non-object top level is an error here rather than a silent empty record
downstream.
"""

import json
from pathlib import Path


class ResultError(Exception):
    """A result file that cannot be read as the JSON a post-processor joins on."""


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
