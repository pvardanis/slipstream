"""Read a cost command's provenance YAML into its validated pydantic model.

Both cost commands take their price and provenance from a per-command YAML file
(ADR-0011): the result files exist only after a run and stay path arguments, while
the reviewable price and provenance cross the pydantic boundary here. This is the
one place the file is read, shaped, and rejected before any figure is priced.
"""

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)


def load_provenance(path: Path, model: type[M], *, error_cls: type[Exception]) -> M:
    """Read the provenance YAML at ``path`` into a validated ``model``.

    A missing, unreadable, non-YAML, empty, or non-mapping file, or a config that
    fails the model's validation, is rejected here as the command's own domain
    error, so the CLI surfaces one exception type rather than a raw
    :class:`pydantic.ValidationError`.

    :param path: the provenance YAML file, e.g. a run's cost provenance.
    :param model: the pydantic model the file's fields validate against.
    :param error_cls: the command's domain error raised on any failure.
    :return: the validated model.
    :raise error_cls: when the file is missing, unreadable, not YAML, empty, not a
        mapping, or fails validation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise error_cls(f"provenance config not found: {path}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise error_cls(
            f"provenance config could not be read: {path}: {error}"
        ) from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise error_cls(f"{path} is not valid YAML: {error}") from error
    # safe_load returns None for an empty or comment-only file without raising; name
    # that here so the command aborts on a clear message, not an opaque "input should
    # be a mapping" from validating None.
    if data is None:
        raise error_cls(f"provenance config is empty: {path}")
    if not isinstance(data, dict):
        raise error_cls(f"provenance config must be a mapping: {path}")
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise error_cls(f"invalid provenance config ({path}):\n{error}") from error
