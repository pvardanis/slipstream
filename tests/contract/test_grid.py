"""Tests for the knob-sweep grid kernel: load, validate, and derive a point's cells.

Covers loading bench/sweep-grid.yaml into the pydantic SweepGrid model, the fail-fast
validation of every swept value before any GPU deploy, deriving one engine point's Tier-2
cells from the grid, and enumerating the Tier-1 engine points — the contract kernel the
executor and the orchestration worker both read from (ADR-0017).
"""

from pathlib import Path

import pytest
import yaml

from slipstream_bench.contract import (
    CellConfig,
    EnginePoint,
    SweepConfig,
    SweepGrid,
    SweepGridError,
    build_point_sweep_config,
    list_engine_points,
    load_grid,
)

REPO_GRID = Path("bench/sweep-grid.yaml")

_LOAD = {
    "total_len": 1000,
    "num_prompts": 500,
    "num_prefixes": 5,
    "output_len": 128,
    "align_blocks": 0,
    "request_rate": 8,
    "seed": 0,
    "goodput": ["ttft:1000", "tpot:50"],
}


def _valid_grid() -> dict[str, object]:
    return {
        "tier1": {
            "max_num_seqs": [16, 32, 64, 128, 256],
            "kv_cache_dtype": ["fp8", "fp16"],
            "prefix_caching": {
                "on": {"flag": "--enable-prefix-caching", "prefix_share": [10, 50, 90]},
                "off": {"flag": "--no-enable-prefix-caching", "prefix_share": [0]},
            },
        },
        "tier2": {"max_concurrency": [8, 16, 32, 64, 128, 256], "burstiness": 1.0},
        "load": dict(_LOAD),
    }


def _write_grid(tmp_path: Path, grid: dict[str, object]) -> Path:
    path = tmp_path / "grid.yaml"
    path.write_text(yaml.safe_dump(grid))
    return path


def test_repo_grid_file_is_valid() -> None:
    """The checked-in grid the recipe reads loads and validates."""
    grid = load_grid(REPO_GRID)
    assert isinstance(grid, SweepGrid)


def test_loads_a_valid_grid(tmp_path: Path) -> None:
    grid = load_grid(_write_grid(tmp_path, _valid_grid()))
    assert grid.tier1.max_num_seqs == [16, 32, 64, 128, 256]
    assert grid.tier2.burstiness == 1.0
    assert grid.load.total_len == 1000
    assert grid.load.goodput == ["ttft:1000", "tpot:50"]


# --- build_point_sweep_config: the grid is the single source of a point's cells --


def _point_config(tmp_path: Path, point_slug: str) -> SweepConfig:
    grid = load_grid(_write_grid(tmp_path, _valid_grid()))
    return build_point_sweep_config(
        grid,
        point_slug,
        base_url="http://127.0.0.1:0",
        model="Qwen/Qwen2.5-0.5B-Instruct",
        out_dir="/out",
        commercial=False,
    )


def test_point_sweep_config_derives_a_caching_on_points_cells(tmp_path: Path) -> None:
    """A caching-on point sweeps its arm's shares x the pinned burstiness x the ladder."""
    config = _point_config(tmp_path, "mns64_kvfp8_pcon")

    coords = [(c.prefix_share, c.burstiness, c.max_concurrency) for c in config.cells()]
    # 3 shares (the 'on' arm) x 1 burstiness x 6 ladder rungs, ladder innermost.
    assert len(coords) == 18
    assert coords[0] == (10, 1.0, 8)
    assert coords[-1] == (90, 1.0, 256)


def test_point_sweep_config_pins_a_caching_off_point_to_the_zero_share(
    tmp_path: Path,
) -> None:
    """A caching-off point sweeps only its single [0] baseline share across the ladder."""
    config = _point_config(tmp_path, "mns32_kvfp16_pcoff")

    shares = {c.prefix_share for c in config.cells()}
    assert shares == {0}
    assert len(list(config.cells())) == 6  # 1 share x 1 burstiness x 6 rungs


def test_point_sweep_config_cells_carry_grid_load_knobs_and_context(
    tmp_path: Path,
) -> None:
    """Each derived cell carries the grid's load knobs and the injected context."""
    config = _point_config(tmp_path, "mns16_kvfp8_pcon")

    cell = next(iter(config.cells()))
    assert isinstance(cell, CellConfig)
    assert cell.total_len == 1000
    assert cell.num_prompts == 500
    assert cell.seed == 0
    assert cell.base_url == "http://127.0.0.1:0"
    assert cell.model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert cell.out_dir == "/out"


@pytest.mark.parametrize(
    ("slug", "mutate"),
    [
        # max_num_seqs the grid does not sweep.
        ("mns999_kvfp8_pcon", lambda g: None),
        # kv_cache_dtype the grid does not sweep.
        ("mns64_kvfp16_pcon", lambda g: g["tier1"].update(kv_cache_dtype=["fp8"])),
        # caching arm the grid does not carry.
        ("mns64_kvfp8_pcoff", lambda g: g["tier1"]["prefix_caching"].pop("off")),
    ],
)
def test_point_sweep_config_rejects_a_point_absent_from_the_grid(
    tmp_path: Path, slug: str, mutate
) -> None:
    """A slug naming knobs the grid does not sweep fails fast, not with wrong cells."""
    grid_dict = _valid_grid()
    mutate(grid_dict)
    grid = load_grid(_write_grid(tmp_path, grid_dict))
    with pytest.raises(SweepGridError, match="not a point this grid sweeps"):
        build_point_sweep_config(
            grid,
            slug,
            base_url="http://127.0.0.1:0",
            model="Qwen/Qwen2.5-0.5B-Instruct",
            out_dir="/out",
            commercial=False,
        )


def test_point_sweep_config_rejects_a_commercial_arm_without_a_tokenizer(
    tmp_path: Path,
) -> None:
    """A commercial point whose grid load pins no tokenizer fails as a grid error."""
    grid = load_grid(_write_grid(tmp_path, _valid_grid()))
    with pytest.raises(SweepGridError, match="tokenizer"):
        build_point_sweep_config(
            grid,
            "mns64_kvfp8_pcon",
            base_url="http://127.0.0.1:0",
            model="gpt-4o-mini",
            out_dir="/out",
            commercial=True,
        )


def test_point_sweep_config_builds_a_commercial_arm_with_a_tokenizer(
    tmp_path: Path,
) -> None:
    """A commercial point whose grid load pins a tokenizer builds its cells."""
    load: dict[str, object] = dict(_LOAD)
    load["tokenizer"] = "Qwen/Qwen2.5-0.5B-Instruct"
    grid_dict = _valid_grid()
    grid_dict["load"] = load
    grid = load_grid(_write_grid(tmp_path, grid_dict))

    config = build_point_sweep_config(
        grid,
        "mns64_kvfp8_pcon",
        base_url="http://127.0.0.1:0",
        model="gpt-4o-mini",
        out_dir="/out",
        commercial=True,
    )

    assert config.commercial is True
    assert config.tokenizer == "Qwen/Qwen2.5-0.5B-Instruct"


def test_point_sweep_config_rejects_a_malformed_slug(tmp_path: Path) -> None:
    """A slug that is not a point subdir name fails fast."""
    with pytest.raises(SweepGridError, match="mns64"):
        _point_config(tmp_path, "mns64")


def test_list_engine_points_enumerates_every_tier1_point(tmp_path: Path) -> None:
    """5 max-num-seqs x 2 kv-dtype x 2 prefix-caching = 20 engine points, as objects."""
    grid = load_grid(_write_grid(tmp_path, _valid_grid()))
    points = list_engine_points(grid)
    assert len(points) == 20
    assert all(isinstance(point, EnginePoint) for point in points)


def test_list_engine_points_yields_the_cartesian_product_of_the_knobs(
    tmp_path: Path,
) -> None:
    """A caching-on fp8 point binds its three engine knobs into one value object."""
    grid = load_grid(_write_grid(tmp_path, _valid_grid()))
    points = list_engine_points(grid)
    assert (
        EnginePoint(max_num_seqs=64, kv_cache_dtype="fp8", prefix_caching=True)
        in points
    )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda g: g["tier1"].update(max_num_seqs=[]), "max_num_seqs"),
        (lambda g: g["tier1"].update(max_num_seqs=[0, 16]), "max_num_seqs"),
        (lambda g: g["tier1"].update(max_num_seqs=[-8]), "max_num_seqs"),
        (lambda g: g["tier1"].update(kv_cache_dtype=[]), "kv_cache_dtype"),
        (lambda g: g["tier1"].update(kv_cache_dtype=["fp8", "fp8"]), "kv_cache_dtype"),
        (lambda g: g["tier1"].update(kv_cache_dtype=["int8"]), "kv_cache_dtype"),
        (lambda g: g["tier1"].update(prefix_caching={}), "prefix_caching"),
        (
            lambda g: g["tier1"]["prefix_caching"]["on"].update(flag=""),
            "flag",
        ),
        (
            lambda g: g["tier1"]["prefix_caching"]["on"].update(prefix_share=[]),
            "prefix_share",
        ),
        (
            lambda g: g["tier1"]["prefix_caching"]["on"].update(prefix_share=[101]),
            "prefix_share",
        ),
        (
            lambda g: g["tier1"]["prefix_caching"]["on"].update(prefix_share=[-1]),
            "prefix_share",
        ),
        (
            lambda g: g["tier1"]["prefix_caching"].update(
                maybe={"flag": "--x", "prefix_share": [0]}
            ),
            "prefix_caching",
        ),
        (lambda g: g["tier2"].update(max_concurrency=[]), "max_concurrency"),
        (lambda g: g["tier2"].update(max_concurrency=[0]), "max_concurrency"),
        (lambda g: g["tier2"].update(burstiness=0), "burstiness"),
        # A repeated swept value collides two points on one results subdir.
        (lambda g: g["tier1"].update(max_num_seqs=[16, 16]), "max_num_seqs"),
        (lambda g: g["tier2"].update(max_concurrency=[8, 8]), "max_concurrency"),
        (
            lambda g: g["tier1"]["prefix_caching"]["on"].update(prefix_share=[10, 10]),
            "prefix_share",
        ),
        # Caching-off reuses no prefix KV, so its share is pinned to the [0]
        # baseline: any other value, or more than one, is a grid mistake.
        (
            lambda g: g["tier1"]["prefix_caching"]["off"].update(prefix_share=[50]),
            "prefix_share",
        ),
        (
            lambda g: g["tier1"]["prefix_caching"]["off"].update(prefix_share=[0, 10]),
            "prefix_share",
        ),
        # A typo'd knob name is rejected, not silently ignored (extra="forbid").
        (lambda g: g["tier1"].update(max_num_seq=[16]), "max_num_seq"),
        (lambda g: g.update(tier3={}), "tier3"),
        # The load section is required: the grid is the single source of a cell's knobs.
        (lambda g: g.pop("load"), "load"),
        (
            lambda g: g["load"].update(num_prompts=2, num_prefixes=5),
            "below num_prefixes",
        ),
        (lambda g: g["load"].update(base_url="x"), "base_url"),
        (
            lambda g: g["tier1"]["prefix_caching"]["on"].update(shares=[10]),
            "shares",
        ),
    ],
)
def test_rejects_an_invalid_grid(tmp_path: Path, mutate, match: str) -> None:
    """Every swept value is validated before the sweep runs, not mid-sweep."""
    grid = _valid_grid()
    mutate(grid)
    with pytest.raises(SweepGridError, match=match):
        load_grid(_write_grid(tmp_path, grid))


def test_rejects_a_grid_that_is_not_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "grid.yaml"
    path.write_text("- not\n- a mapping\n")
    with pytest.raises(SweepGridError):
        load_grid(path)


def test_rejects_malformed_yaml(tmp_path: Path) -> None:
    """Broken YAML syntax fails loud, not as a raw scanner traceback."""
    path = tmp_path / "grid.yaml"
    path.write_text("tier1: [unclosed\n")
    with pytest.raises(SweepGridError, match="not valid YAML"):
        load_grid(path)


def test_reports_an_empty_grid_file(tmp_path: Path) -> None:
    path = tmp_path / "grid.yaml"
    path.write_text("")
    with pytest.raises(SweepGridError, match="empty"):
        load_grid(path)


def test_reports_a_missing_grid_file(tmp_path: Path) -> None:
    with pytest.raises(SweepGridError, match="not found"):
        load_grid(tmp_path / "absent.yaml")


def test_reports_an_unreadable_grid_path(tmp_path: Path) -> None:
    """A directory handed as --grid fails as a read error, not a traceback."""
    with pytest.raises(SweepGridError, match="could not be read"):
        load_grid(tmp_path)
