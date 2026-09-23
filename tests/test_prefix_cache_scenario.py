"""Tests for the prefix-cache scenario boundary: read a YAML file into a model.

Covers the read/parse/shape guards the loader applies before validation — a
missing, unreadable, empty, non-mapping, or non-YAML file — that the cache regime
is one of the two defined labels (validated once, in the model), that the model
selector is optional, and that a validation failure surfaces as the command's own
PrefixCacheError rather than a raw ValidationError.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from slipstream_bench.prefix_cache import (
    PrefixCacheError,
    PrefixCacheScenario,
    load_prefix_cache_scenario,
)

_SCENARIO = {"cache_state": "cold", "model": "served-name"}


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_load_reads_a_valid_yaml(tmp_path: Path) -> None:
    """A complete scenario YAML validates into PrefixCacheScenario."""
    scenario = load_prefix_cache_scenario(_write(tmp_path, _SCENARIO))

    assert scenario.cache_state == "cold"
    assert scenario.model == "served-name"


def test_model_selector_is_optional(tmp_path: Path) -> None:
    """Omitting model defaults it to None so scrape derives it from the result."""
    scenario = load_prefix_cache_scenario(_write(tmp_path, {"cache_state": "warm"}))

    assert scenario.cache_state == "warm"
    assert scenario.model is None


@pytest.mark.parametrize("state", ["lukewarm", "", "COLD"])
def test_bad_cache_state_fails_model_validate(state: str) -> None:
    """The label is the whole cold-vs-warm basis; a free-text value is rejected."""
    with pytest.raises(ValidationError, match="cache_state"):
        PrefixCacheScenario.model_validate({"cache_state": state})


@pytest.mark.parametrize("model", ["", "   ", "\t\n"])
def test_blank_model_selector_is_rejected(model: str) -> None:
    """A blank selector is truthy at scrape's model-or-default step yet names no
    series, so it is rejected at the boundary rather than deferred to a scrape miss.
    """
    with pytest.raises(ValidationError, match="model"):
        PrefixCacheScenario.model_validate({"cache_state": "cold", "model": model})


def test_unknown_key_fails_model_validate() -> None:
    """A typo in the reviewed artifact fails loudly rather than being ignored."""
    with pytest.raises(ValidationError, match="cache_stat"):
        PrefixCacheScenario.model_validate({"cache_stat": "cold"})


def test_bad_cache_state_via_loader_is_a_domain_error(tmp_path: Path) -> None:
    """A bad label read through the loader surfaces as PrefixCacheError."""
    with pytest.raises(PrefixCacheError, match="cache_state"):
        load_prefix_cache_scenario(_write(tmp_path, {"cache_state": "lukewarm"}))


def test_missing_file_raises_domain_error(tmp_path: Path) -> None:
    """A path that does not exist is the command's own error, not FileNotFoundError."""
    with pytest.raises(PrefixCacheError, match="not found"):
        load_prefix_cache_scenario(tmp_path / "absent.yaml")


def test_unreadable_file_raises_domain_error(tmp_path: Path) -> None:
    """A non-UTF-8 file surfaces as the domain error, not a raw UnicodeDecodeError."""
    path = tmp_path / "scenario.yaml"
    path.write_bytes(b"\xff\xfe\x00 not utf-8")

    with pytest.raises(PrefixCacheError, match="could not be read"):
        load_prefix_cache_scenario(path)


def test_empty_config_is_rejected(tmp_path: Path) -> None:
    """An empty file names itself rather than failing opaquely on a None validate."""
    path = tmp_path / "scenario.yaml"
    path.write_text("")

    with pytest.raises(PrefixCacheError, match="empty"):
        load_prefix_cache_scenario(path)


def test_non_mapping_config_is_rejected(tmp_path: Path) -> None:
    """A YAML list is not the mapping the model validates."""
    path = tmp_path / "scenario.yaml"
    path.write_text("- a\n- b\n")

    with pytest.raises(PrefixCacheError, match="mapping"):
        load_prefix_cache_scenario(path)


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    """Unparseable YAML fails as a read error, not a validation error."""
    path = tmp_path / "scenario.yaml"
    path.write_text("a: [1, 2\n")

    with pytest.raises(PrefixCacheError, match="not valid YAML"):
        load_prefix_cache_scenario(path)
