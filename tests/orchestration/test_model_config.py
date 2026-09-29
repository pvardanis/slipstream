"""The served model id is read from model.yaml, the model source of truth.

ADR-0012: model.yaml pins the model the rig serves and the bench client measures. The
orchestration driver reads ``.model.hfId`` from it rather than taking a ``--model`` flag
that could silently disagree with the file the digest is taken over.
"""

from pathlib import Path

import pytest

from slipstream_bench.orchestration.model_config import (
    ModelConfigError,
    read_model_id,
)

_MODEL_YAML = "model:\n  hfId: Qwen/Qwen3-8B-AWQ\n  revision: abc123\n"


def test_read_model_id_returns_the_hf_id(tmp_path: Path) -> None:
    model_yaml = tmp_path / "model.yaml"
    model_yaml.write_text(_MODEL_YAML, encoding="utf-8")

    assert read_model_id(model_yaml) == "Qwen/Qwen3-8B-AWQ"


def test_read_model_id_strips_surrounding_whitespace(tmp_path: Path) -> None:
    model_yaml = tmp_path / "model.yaml"
    model_yaml.write_text("model:\n  hfId: '  Qwen/Qwen3-8B-AWQ  '\n", encoding="utf-8")

    assert read_model_id(model_yaml) == "Qwen/Qwen3-8B-AWQ"


def test_read_model_id_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ModelConfigError, match="not found"):
        read_model_id(tmp_path / "absent.yaml")


def test_read_model_id_rejects_invalid_yaml(tmp_path: Path) -> None:
    model_yaml = tmp_path / "model.yaml"
    model_yaml.write_text("model: [unterminated\n", encoding="utf-8")

    with pytest.raises(ModelConfigError, match="not valid YAML"):
        read_model_id(model_yaml)


def test_read_model_id_rejects_a_missing_hf_id(tmp_path: Path) -> None:
    model_yaml = tmp_path / "model.yaml"
    model_yaml.write_text("model:\n  revision: abc123\n", encoding="utf-8")

    with pytest.raises(ModelConfigError, match="hfId"):
        read_model_id(model_yaml)
