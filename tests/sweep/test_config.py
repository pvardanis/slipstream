"""Tests for the load-sweep config model and its YAML loader.

The rejection matrix drives ``SweepConfig.model_validate`` directly: an empty grid
axis, an out-of-range share, a duplicate, a nonpositive cap, fewer prompts than
prefixes, a commercial arm with no tokenizer, and a bad request rate each fail
validation. The loader tests drive ``load_sweep_config``: it binds the CLI-injected
execution context onto the YAML experiment definition, guards the reserved keys the
config file must never set, and wraps every read/parse/validate failure in a
``SweepError`` with exit-worthy text.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from slipstream_bench.sweep.config import (
    SweepConfig,
    SweepError,
    load_sweep_config,
)

# The experiment-defining knobs the config YAML carries; the loader injects the
# execution context (base_url, model, out_dir, commercial) on top of these.
_EXPERIMENT = {
    "prefix_shares": [10, 50, 90],
    "burstiness_values": [0.2, 1.0],
    "total_len": 1000,
    "num_prompts": 100,
    "num_prefixes": 5,
    "output_len": 128,
    "align_blocks": 0,
    "request_rate": 8,
    "seed": 0,
    "goodput": ["ttft:1000", "tpot:50"],
}

_CONTEXT = {
    "base_url": "http://localhost:8000",
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "out_dir": "bench/results",
    "commercial": False,
}


def _valid(**overrides: object) -> dict[str, object]:
    """A full, valid model payload (experiment + context) with overrides applied."""
    return {**_EXPERIMENT, **_CONTEXT, **overrides}


def _write_config(tmp_path: Path, **overrides: object) -> Path:
    """Write an experiment-only config YAML (no reserved keys) and return its path."""
    path = tmp_path / "load-sweep.yaml"
    path.write_text(yaml.safe_dump({**_EXPERIMENT, **overrides}))
    return path


# --- the model validates a good config --------------------------------------


def test_a_full_config_validates() -> None:
    """The documented defaults validate into a frozen config."""
    config = SweepConfig.model_validate(_valid())

    assert config.prefix_shares == [10, 50, 90]
    assert config.request_rate == "8"  # a YAML int is coerced to the flag's string
    assert config.max_concurrency_values == []  # open-loop unless a ladder is given


def test_the_model_is_frozen() -> None:
    """A validated config cannot be mutated after construction."""
    config = SweepConfig.model_validate(_valid())
    with pytest.raises(ValidationError):
        config.seed = 1  # type: ignore[misc]


# --- the rejection matrix ----------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"prefix_shares": []}, "prefix_shares"),
        ({"burstiness_values": []}, "burstiness_values"),
        ({"goodput": []}, "goodput"),
        ({"prefix_shares": [150]}, "less than or equal to 100"),
        ({"prefix_shares": [-5]}, "greater than or equal to 0"),
        ({"prefix_shares": [10, 50, 10]}, "unique"),
        ({"burstiness_values": [0.2, 1.0, 0.2]}, "unique"),
        ({"burstiness_values": [0]}, "greater than 0"),
        ({"max_concurrency_values": [0]}, "greater than 0"),
        ({"max_concurrency_values": [8, 16, 8]}, "unique"),
        ({"total_len": 0}, "greater than 0"),
        ({"num_prompts": 2, "num_prefixes": 5}, "below num_prefixes"),
        ({"align_blocks": -16}, "greater than or equal to 0"),
        ({"request_rate": "quick"}, "non-negative number or 'inf'"),
        ({"request_rate": -8}, "non-negative number or 'inf'"),
        ({"request_rate": "nan"}, "non-negative number or 'inf'"),
        ({"commercial": True}, "tokenizer"),
        ({"unknown_knob": 1}, "Extra inputs are not permitted"),
    ],
)
def test_model_rejects_a_bad_config(overrides: dict[str, object], match: str) -> None:
    """Each malformed knob fails validation with an identifying message."""
    with pytest.raises(ValidationError, match=match):
        SweepConfig.model_validate(_valid(**overrides))


def test_request_rate_inf_is_accepted() -> None:
    """'inf' is a legitimate unthrottled sweep."""
    assert SweepConfig.model_validate(_valid(request_rate="inf")).request_rate == "inf"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(8.5, "8.5"), (float("inf"), "inf")],
)
def test_request_rate_coerces_a_yaml_float(value: float, expected: str) -> None:
    """A YAML float (finite or .inf) is coerced to the string the flag carries."""
    assert (
        SweepConfig.model_validate(_valid(request_rate=value)).request_rate == expected
    )


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_commercial_arm_rejects_a_blank_tokenizer(blank: str) -> None:
    """A whitespace-only tokenizer pins no real ruler; the commercial arm rejects it."""
    with pytest.raises(ValidationError, match="tokenizer"):
        SweepConfig.model_validate(_valid(commercial=True, tokenizer=blank))


def test_commercial_arm_with_a_tokenizer_validates() -> None:
    """A commercial run pinned to a local tokenizer validates."""
    SweepConfig.model_validate(
        _valid(commercial=True, tokenizer="Qwen/Qwen2.5-0.5B-Instruct")
    )


# --- the loader: bind context, guard reserved keys, wrap failures ------------


def test_load_binds_the_cli_injected_context(tmp_path: Path) -> None:
    """The loader injects base_url/model/out_dir/commercial onto the YAML knobs."""
    path = _write_config(tmp_path)

    config = load_sweep_config(
        path,
        base_url="http://127.0.0.1:9",
        model="Qwen/Qwen3-8B-AWQ",
        out_dir="/out",
        commercial=False,
    )

    assert config.base_url == "http://127.0.0.1:9"
    assert config.model == "Qwen/Qwen3-8B-AWQ"
    assert config.out_dir == "/out"
    assert config.prefix_shares == [10, 50, 90]


@pytest.mark.parametrize("key", ["base_url", "model", "out_dir", "commercial"])
def test_load_rejects_a_config_that_sets_a_reserved_key(
    tmp_path: Path, key: str
) -> None:
    """A config file may not set a CLI-injected key; model.yaml is the model SoT."""
    path = _write_config(tmp_path, **{key: "x"})

    with pytest.raises(SweepError, match=key):
        load_sweep_config(
            path, base_url="u", model="m", out_dir="/out", commercial=False
        )


def test_load_reports_a_missing_config(tmp_path: Path) -> None:
    """A missing config file fails fast with the path, not a traceback."""
    with pytest.raises(SweepError, match="not found"):
        load_sweep_config(
            tmp_path / "absent.yaml",
            base_url="u",
            model="m",
            out_dir="/out",
            commercial=False,
        )


def test_load_reports_an_unreadable_config(tmp_path: Path) -> None:
    """A path that is a directory, not a file, fails as unreadable with the path."""
    with pytest.raises(SweepError, match="could not be read"):
        load_sweep_config(
            tmp_path, base_url="u", model="m", out_dir="/out", commercial=False
        )


@pytest.mark.parametrize(
    ("document", "match"),
    [("- 1\n- 2\n", "must be a mapping"), ("hello\n", "must be a mapping")],
)
def test_load_rejects_a_non_mapping_config(
    tmp_path: Path, document: str, match: str
) -> None:
    """A top-level list or scalar is named a mapping error, not an opaque one."""
    path = tmp_path / "not-a-map.yaml"
    path.write_text(document)

    with pytest.raises(SweepError, match=match):
        load_sweep_config(
            path, base_url="u", model="m", out_dir="/out", commercial=False
        )


def test_load_reports_an_empty_config(tmp_path: Path) -> None:
    """An empty (or comment-only) config is named, not validated as None."""
    path = tmp_path / "empty.yaml"
    path.write_text("# only a comment\n")

    with pytest.raises(SweepError, match="empty"):
        load_sweep_config(
            path, base_url="u", model="m", out_dir="/out", commercial=False
        )


def test_load_reports_invalid_yaml(tmp_path: Path) -> None:
    """A malformed YAML document is reported as such."""
    path = tmp_path / "bad.yaml"
    path.write_text("prefix_shares: [1, 2\n")

    with pytest.raises(SweepError, match="not valid YAML"):
        load_sweep_config(
            path, base_url="u", model="m", out_dir="/out", commercial=False
        )


def test_load_wraps_a_validation_failure(tmp_path: Path) -> None:
    """A schema-invalid config is wrapped in a SweepError naming the file."""
    path = _write_config(tmp_path, prefix_shares=[150])

    with pytest.raises(SweepError, match="invalid sweep config"):
        load_sweep_config(
            path, base_url="u", model="m", out_dir="/out", commercial=False
        )
