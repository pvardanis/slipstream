"""Read the served model's identity from model.yaml, the model source of truth.

ADR-0012: model.yaml is the single source of truth for the model the GPU rig serves and
the bench client measures; its ``.model.hfId`` is the Hugging Face id the sweep runs
against. The orchestration driver reads it here rather than taking a flag that could
silently disagree with the file the deep config digest is taken over.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class ModelConfigError(Exception):
    """A model.yaml that cannot yield the served model id."""


def read_model_id(model_yaml: Path) -> str:
    """Read the served model's Hugging Face id from model.yaml.

    :param model_yaml: the ``model.yaml`` source of truth.
    :return: the ``.model.hfId`` value, e.g. ``Qwen/Qwen3-8B-AWQ``.
    :raise ModelConfigError: when model.yaml is missing, unreadable, not valid YAML,
        carries no ``.model`` mapping, or declares no non-empty ``.model.hfId``.
    """
    try:
        text = model_yaml.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ModelConfigError(f"model.yaml not found: {model_yaml}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise ModelConfigError(
            f"model.yaml could not be read: {model_yaml}: {error}"
        ) from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ModelConfigError(f"{model_yaml} is not valid YAML: {error}") from error
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, dict):
        raise ModelConfigError(f"{model_yaml} carries no .model mapping")
    hf_id = model.get("hfId")
    if not isinstance(hf_id, str) or not hf_id.strip():
        raise ModelConfigError(
            f"{model_yaml} declares no non-empty .model.hfId to serve"
        )
    return hf_id.strip()
