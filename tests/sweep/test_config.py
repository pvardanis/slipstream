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
    CellConfig,
    SweepConfig,
    SweepError,
    load_cell_config,
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
        config.seed = 1  # ty: ignore[invalid-assignment]  # asserting the frozen model rejects the write


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


# The shared knobs a cell YAML carries (no grid axes); the coordinate is layered on.
_CELL_KNOBS = {
    "total_len": 1000,
    "num_prompts": 100,
    "num_prefixes": 5,
    "output_len": 128,
    "align_blocks": 0,
    "request_rate": 8,
    "seed": 0,
    "goodput": ["ttft:1000", "tpot:50"],
}


def _valid_cell(**overrides: object) -> dict[str, object]:
    """A full, valid CellConfig payload (knobs + coordinate + context) with overrides."""
    return {
        **_CELL_KNOBS,
        **_CONTEXT,
        "share": 50,
        "burstiness": 1.0,
        **overrides,
    }


def _write_cell(tmp_path: Path, **overrides: object) -> Path:
    """Write a cell-only config YAML (no reserved keys) and return its path."""
    path = tmp_path / "cell.yaml"
    payload = {**_CELL_KNOBS, "share": 50, "burstiness": 1.0, **overrides}
    path.write_text(yaml.safe_dump(payload))
    return path


# --- CellConfig: the coordinate is the type's job to range-check ---------------


def test_a_full_cell_validates() -> None:
    """A cell with a valid coordinate validates into a frozen config."""
    cell = CellConfig.model_validate(_valid_cell())

    assert cell.share == 50
    assert cell.burstiness == 1.0
    assert cell.max_concurrency is None  # open-loop unless a cap is given


def test_a_closed_loop_cell_carries_its_cap() -> None:
    """A max_concurrency cap validates as a positive in-flight cap."""
    assert (
        CellConfig.model_validate(_valid_cell(max_concurrency=64)).max_concurrency == 64
    )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"share": 150}, "less than or equal to 100"),
        ({"share": -5}, "greater than or equal to 0"),
        ({"burstiness": 0}, "greater than 0"),
        ({"burstiness": -0.5}, "greater than 0"),
        ({"max_concurrency": 0}, "greater than 0"),
        ({"max_concurrency": -4}, "greater than 0"),
        ({"num_prompts": 2, "num_prefixes": 5}, "below num_prefixes"),
        ({"commercial": True}, "tokenizer"),
        ({"prefix_shares": [10, 50]}, "Extra inputs are not permitted"),
        ({"unknown_knob": 1}, "Extra inputs are not permitted"),
    ],
)
def test_cell_rejects_a_bad_config(overrides: dict[str, object], match: str) -> None:
    """A coordinate out of range, or a bad shared knob, fails validation."""
    with pytest.raises(ValidationError, match=match):
        CellConfig.model_validate(_valid_cell(**overrides))


def test_cell_edges_validate() -> None:
    """The grid's edge coordinates pass: share 0 and 100, a positive cap."""
    CellConfig.model_validate(_valid_cell(share=0, burstiness=0.001))
    CellConfig.model_validate(_valid_cell(share=100, max_concurrency=1))


# --- SweepConfig.cells(): the grid derives one CellConfig per point ------------


def test_cells_yields_one_open_loop_cell_per_share_burstiness() -> None:
    """With no ladder the grid is one open-loop cell per (share, burstiness), ordered."""
    config = SweepConfig.model_validate(
        _valid(prefix_shares=[10, 90], burstiness_values=[0.2, 1.0])
    )

    coords = [(c.share, c.burstiness, c.max_concurrency) for c in config.cells()]
    assert coords == [
        (10, 0.2, None),
        (10, 1.0, None),
        (90, 0.2, None),
        (90, 1.0, None),
    ]


def test_cells_ladders_max_concurrency_innermost() -> None:
    """The ladder is innermost: its rungs run contiguously within one (share, burstiness)."""
    config = SweepConfig.model_validate(
        _valid(
            prefix_shares=[10, 90],
            burstiness_values=[0.2, 1.0],
            max_concurrency_values=[8, 16],
        )
    )

    coords = [(c.share, c.burstiness, c.max_concurrency) for c in config.cells()]
    assert coords == [
        (10, 0.2, 8),
        (10, 0.2, 16),
        (10, 1.0, 8),
        (10, 1.0, 16),
        (90, 0.2, 8),
        (90, 0.2, 16),
        (90, 1.0, 8),
        (90, 1.0, 16),
    ]


def test_cells_carry_the_shared_knobs() -> None:
    """Each derived cell carries the sweep's shared knobs and execution context."""
    config = SweepConfig.model_validate(
        _valid(prefix_shares=[90], burstiness_values=[1.0], tokenizer="tok")
    )

    (cell,) = list(config.cells())
    assert isinstance(cell, CellConfig)
    assert cell.total_len == 1000
    assert cell.seed == 0
    assert cell.base_url == "http://localhost:8000"
    assert cell.model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert cell.out_dir == "bench/results"
    assert cell.tokenizer == "tok"


def test_laddered_cells_carry_the_shared_knobs_and_commercial_flag() -> None:
    """Every laddered cell carries the shared knobs and the commercial flag verbatim."""
    config = SweepConfig.model_validate(
        _valid(
            prefix_shares=[10, 90],
            burstiness_values=[1.0],
            max_concurrency_values=[8, 16],
            commercial=True,
            tokenizer="tok",
        )
    )

    cells = list(config.cells())
    assert len(cells) == 4  # 2 shares x 1 burstiness x 2 ladder rungs
    # The commercial flag and shared knobs survive model_dump onto every derived cell.
    assert all(c.commercial is True for c in cells)
    assert all(c.tokenizer == "tok" for c in cells)
    assert all(c.total_len == 1000 and c.seed == 0 for c in cells)


# --- load_cell_config: bind context, guard reserved keys, wrap failures --------


def test_load_cell_binds_the_cli_injected_context(tmp_path: Path) -> None:
    """The cell loader injects base_url/model/out_dir/commercial onto the YAML knobs."""
    path = _write_cell(tmp_path, share=90, burstiness=0.2)

    cell = load_cell_config(
        path,
        base_url="http://127.0.0.1:9",
        model="Qwen/Qwen3-8B-AWQ",
        out_dir="/out",
        commercial=False,
    )

    assert cell.base_url == "http://127.0.0.1:9"
    assert cell.model == "Qwen/Qwen3-8B-AWQ"
    assert cell.out_dir == "/out"
    assert cell.share == 90
    assert cell.burstiness == 0.2


@pytest.mark.parametrize("key", ["base_url", "model", "out_dir", "commercial"])
def test_load_cell_rejects_a_reserved_key(tmp_path: Path, key: str) -> None:
    """A cell config may not set a CLI-injected key; model.yaml is the model SoT."""
    path = _write_cell(tmp_path, **{key: "x"})

    with pytest.raises(SweepError, match=key):
        load_cell_config(
            path, base_url="u", model="m", out_dir="/out", commercial=False
        )


def test_load_cell_reports_a_missing_config(tmp_path: Path) -> None:
    """A missing cell config fails fast with the path, not a traceback."""
    with pytest.raises(SweepError, match="not found"):
        load_cell_config(
            tmp_path / "absent.yaml",
            base_url="u",
            model="m",
            out_dir="/out",
            commercial=False,
        )


def test_load_cell_wraps_a_validation_failure(tmp_path: Path) -> None:
    """A schema-invalid cell config is wrapped in a SweepError naming the file."""
    path = _write_cell(tmp_path, share=150)

    with pytest.raises(SweepError, match="invalid cell config"):
        load_cell_config(
            path, base_url="u", model="m", out_dir="/out", commercial=False
        )
