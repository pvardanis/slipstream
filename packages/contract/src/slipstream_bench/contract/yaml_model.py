"""Read a YAML config file and validate it against a pydantic model, failing as one error.

The kernel's config files — the knob-sweep grid, the kv-cache-extension-ab workload — are
each a YAML mapping validated by a pydantic model. This reads and validates one, turning
every way the file can fail — missing, unreadable, empty, not YAML, invalid — into the
caller's own error type, so the whole file is checked before a sweep touches a GPU.
"""

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)


def load_yaml_model(
    path: Path, model: type[M], *, label: str, error: type[Exception]
) -> M:
    """Read the YAML file at ``path`` and validate it as ``model``.

    :param path: the config YAML file.
    :param model: the pydantic model the file's mapping validates against.
    :param label: what the file is, for the error messages (e.g. ``sweep grid``).
    :param error: the exception type every failure is raised as.
    :return: the validated model.
    :raise error: when the file is missing, unreadable, empty, not valid YAML, or fails
        validation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as cause:
        raise error(f"{label} not found: {path}") from cause
    except (OSError, UnicodeDecodeError) as cause:
        raise error(f"{label} could not be read: {path}: {cause}") from cause
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as cause:
        raise error(f"{label} {path} is not valid YAML: {cause}") from cause
    # safe_load returns None for an empty or comment-only file without raising;
    # name that here so the sweep aborts on a clear message, not an opaque
    # "input should be a mapping" from validating None.
    if data is None:
        raise error(f"{label} is empty: {path}")
    try:
        # model_validate, not model(**data): a list or scalar top level would
        # make **data raise TypeError past this handler; model_validate turns any
        # non-mapping into the ValidationError we wrap.
        return model.model_validate(data)
    except ValidationError as cause:
        raise error(f"invalid {label} ({path}):\n{cause}") from cause
