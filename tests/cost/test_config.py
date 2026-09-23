"""Tests for the cost provenance-config boundary: read a YAML file into a model.

Covers the read/parse/shape guards the shared loader applies before validation —
a missing, empty, non-mapping, or non-YAML file — and that a model validation
failure surfaces as the command's own domain error, not a raw ValidationError.
"""

from pathlib import Path

import pytest
import yaml

from slipstream_bench.cost.commercial import (
    CommercialCostError,
    load_commercial_cost_inputs,
)
from slipstream_bench.cost.self_hosted import CostError, load_cost_inputs

_COST = {
    "price_per_hour": 2.0,
    "output_input_ratio": 1.0,
    "weight_checksum": "sha256:deadbeef",
    "vllm_version": "0.6.3",
    "quant_recipe": "awq_marlin+fp8-kv",
}

_COMMERCIAL = {
    "input_price_per_1m": 0.5,
    "output_price_per_1m": 1.5,
    "api": "openai",
    "model": "gpt-4o-mini",
    "price_quoted_on": "2026-09-11",
}


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "provenance.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_load_cost_inputs_reads_a_valid_yaml(tmp_path: Path) -> None:
    """A complete cost YAML validates into CostInputs."""
    inputs = load_cost_inputs(_write(tmp_path, _COST))

    assert inputs.price_per_hour == 2.0
    assert inputs.weight_checksum == "sha256:deadbeef"


def test_load_commercial_reads_a_valid_yaml(tmp_path: Path) -> None:
    """A complete commercial YAML validates into CommercialCostInputs."""
    inputs = load_commercial_cost_inputs(_write(tmp_path, _COMMERCIAL))

    assert inputs.input_price_per_1m == 0.5
    assert inputs.api == "openai"


def test_unquoted_yaml_date_loads(tmp_path: Path) -> None:
    """An unquoted quote date parses to a YAML date but keeps its ISO text."""
    path = tmp_path / "provenance.yaml"
    # An unquoted 2026-09-11 parses to a datetime.date, unlike safe_dump's quoting.
    path.write_text(
        "input_price_per_1m: 0.5\n"
        "output_price_per_1m: 1.5\n"
        "api: openai\n"
        "model: gpt-4o-mini\n"
        "price_quoted_on: 2026-09-11\n"
    )

    inputs = load_commercial_cost_inputs(path)

    assert inputs.price_quoted_on == "2026-09-11"


def test_missing_file_raises_domain_error(tmp_path: Path) -> None:
    """A path that does not exist is the command's own error, not FileNotFoundError."""
    with pytest.raises(CostError, match="not found"):
        load_cost_inputs(tmp_path / "absent.yaml")


def test_unreadable_file_raises_domain_error(tmp_path: Path) -> None:
    """A non-UTF-8 file surfaces as the domain error, not a raw UnicodeDecodeError."""
    path = tmp_path / "provenance.yaml"
    path.write_bytes(b"\xff\xfe\x00 not utf-8")

    with pytest.raises(CostError, match="could not be read"):
        load_cost_inputs(path)


def test_empty_config_is_rejected(tmp_path: Path) -> None:
    """An empty file names itself rather than failing opaquely on a None validate."""
    path = tmp_path / "provenance.yaml"
    path.write_text("")

    with pytest.raises(CostError, match="empty"):
        load_cost_inputs(path)


def test_non_mapping_config_is_rejected(tmp_path: Path) -> None:
    """A YAML list is not the mapping the model validates."""
    path = tmp_path / "provenance.yaml"
    path.write_text("- a\n- b\n")

    with pytest.raises(CostError, match="mapping"):
        load_cost_inputs(path)


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    """Unparseable YAML fails as a read error, not a validation error."""
    path = tmp_path / "provenance.yaml"
    path.write_text("a: [1, 2\n")

    with pytest.raises(CostError, match="not valid YAML"):
        load_cost_inputs(path)


def test_validation_failure_is_wrapped_as_domain_error(tmp_path: Path) -> None:
    """A bad field surfaces as CostError wrapping the ValidationError text."""
    with pytest.raises(CostError, match="price_per_hour"):
        load_cost_inputs(_write(tmp_path, {**_COST, "price_per_hour": 0}))


def test_commercial_validation_failure_is_wrapped(tmp_path: Path) -> None:
    """A bad commercial field surfaces as CommercialCostError."""
    with pytest.raises(CommercialCostError, match="price_quoted_on"):
        load_commercial_cost_inputs(
            _write(tmp_path, {**_COMMERCIAL, "price_quoted_on": "last tuesday"})
        )
