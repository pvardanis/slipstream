"""The cell run's Prefect tags: one filterable key=value per tier1 and tier2 knob.

Covers the tier1 knobs parsed off the point slug, the tier2 knobs read off the cell,
and the open-loop cell whose absent concurrency cap tags ``mc=open``.
"""

from slipstream_bench.orchestration.task_labels import cell_run_tags
from slipstream_bench.sweep.config import CellConfig

_SHARED_KNOBS = {
    "base_url": "http://localhost:8000",
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "total_len": 1000,
    "num_prompts": 100,
    "num_prefixes": 5,
    "output_len": 128,
    "align_blocks": 0,
    "request_rate": "8",
    "seed": 0,
    "out_dir": "bench/results",
    "goodput": ["ttft:1000", "tpot:50"],
}


def _cell(**overrides: object) -> CellConfig:
    base = {**_SHARED_KNOBS, "prefix_share": 50, "burstiness": 1.0}
    base.update(overrides)
    return CellConfig(**base)  # ty: ignore[invalid-argument-type]  # dynamic kwargs spread from an object-valued dict


def test_a_closed_loop_cell_tags_each_tier1_and_tier2_knob() -> None:
    tags = cell_run_tags("mns64_kvfp8_pcon", _cell(max_concurrency=64))

    assert tags == [
        "mns=64",
        "kv=fp8",
        "pc=on",
        "pshare=50",
        "burst=1.0",
        "mc=64",
    ]


def test_prefix_caching_off_tags_pc_off() -> None:
    tags = cell_run_tags("mns128_kvfp16_pcoff", _cell(prefix_share=0))

    assert "pc=off" in tags
    assert "kv=fp16" in tags
    assert "mns=128" in tags


def test_an_open_loop_cell_tags_mc_open() -> None:
    tags = cell_run_tags("mns64_kvfp8_pcon", _cell(max_concurrency=None))

    assert "mc=open" in tags
