"""Tests for the cost post-processor: run cost split into $/1M in/out.

Seeded from the checklist mined off the deleted bash test (ADR-0003): the
split arithmetic, the ratio weighting, fractional figures, order-preserving
multi-file output, run-to-run reproducibility, the pinned provenance, and the
fail-fast guards on price, ratio, blank provenance, missing/null/non-numeric/
non-finite/negative metrics, a non-positive duration, and a zero-token
denominator.
"""

import json
from pathlib import Path

import pytest

from slipstream_bench.cost import CostError, CostInputs, price_files, price_result

PINS = {
    "weight_checksum": "sha256:deadbeef",
    "vllm_version": "0.6.3",
    "quant_recipe": "awq_marlin+fp8-kv",
}


def _inputs(
    *, price_per_hour: float = 2.0, output_input_ratio: float = 1.0
) -> CostInputs:
    return CostInputs(
        price_per_hour=price_per_hour,
        output_input_ratio=output_input_ratio,
        **PINS,
    )


def _record(
    *,
    duration: float = 3600.0,
    total_input_tokens: object = 1_000_000,
    total_output_tokens: object = 0,
    **extra: object,
) -> dict:
    record = {
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "duration": duration,
        "completed": 100,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }
    record.update(extra)
    return record


def test_run_cost_is_wallclock_times_hourly_price() -> None:
    """One hour at $2/hr is a $2 run; ratio 1, 1M input tokens -> $2/1M input."""
    priced = price_result(_record(), Path("cell.json"), _inputs())

    assert priced["run_cost_usd"] == pytest.approx(2.0)
    assert priced["cost_per_1m_input_usd"] == pytest.approx(2.0)
    # No output tokens, but the reported output price tracks the ratio share.
    assert priced["cost_per_1m_output_usd"] == pytest.approx(2.0)


def test_ratio_weights_output_tokens_heavier() -> None:
    """1M in + 1M out, $4/hr, ratio 3: denom 4e6 -> $1/1M in, $3/1M out."""
    priced = price_result(
        _record(total_input_tokens=1_000_000, total_output_tokens=1_000_000),
        Path("split.json"),
        _inputs(price_per_hour=4.0, output_input_ratio=3.0),
    )

    assert priced["cost_per_1m_input_usd"] == pytest.approx(1.0)
    assert priced["cost_per_1m_output_usd"] == pytest.approx(3.0)


def test_fractional_figures_are_exact() -> None:
    """Half an hour at $1.50/hr, 500k in + 250k out, ratio 2 -> $0.75 in, $1.50 out."""
    priced = price_result(
        _record(
            duration=1800.0, total_input_tokens=500_000, total_output_tokens=250_000
        ),
        Path("frac.json"),
        _inputs(price_per_hour=1.50, output_input_ratio=2.0),
    )

    assert priced["run_cost_usd"] == pytest.approx(0.75)
    assert priced["cost_per_1m_input_usd"] == pytest.approx(0.75)
    assert priced["cost_per_1m_output_usd"] == pytest.approx(1.50)


def test_provenance_and_bench_metrics_ride_on_every_record() -> None:
    """The pinned provenance triple and echoed bench metrics are self-describing."""
    priced = price_result(_record(), Path("cell.json"), _inputs())

    assert priced["weight_checksum"] == "sha256:deadbeef"
    assert priced["vllm_version"] == "0.6.3"
    assert priced["quant_recipe"] == "awq_marlin+fp8-kv"
    assert priced["output_input_ratio"] == 1.0
    assert priced["price_per_hour_usd"] == 2.0
    assert priced["model_id"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert priced["total_input_tokens"] == 1_000_000
    assert priced["source"] == "cell.json"


def test_price_files_emits_one_record_per_file_in_order(tmp_path: Path) -> None:
    """A cost record per input file, in the order given."""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps(_record()))
    second.write_text(json.dumps(_record(total_output_tokens=1_000_000)))

    records = price_files([first, second], _inputs())

    assert [r["source"] for r in records] == [str(first), str(second)]


def test_same_input_reproduces_identical_output(tmp_path: Path) -> None:
    """A re-run reproduces the figure byte for byte (acceptance criterion 3)."""
    result = tmp_path / "cell.json"
    result.write_text(json.dumps(_record()))

    run_a = json.dumps(price_files([result], _inputs()))
    run_b = json.dumps(price_files([result], _inputs()))

    assert run_a == run_b


def test_non_positive_price_is_rejected() -> None:
    """A zero price is a free-GPU fiction; reject it before the arithmetic."""
    with pytest.raises(CostError, match="price-per-hour"):
        _inputs(price_per_hour=0.0)


def test_non_positive_ratio_is_rejected() -> None:
    """A non-positive ratio inverts the split; reject it before the arithmetic."""
    with pytest.raises(CostError, match="output-input-ratio"):
        _inputs(output_input_ratio=-1.0)


@pytest.mark.parametrize(
    "metric", ["duration", "total_input_tokens", "total_output_tokens"]
)
def test_missing_metric_is_rejected(metric: str) -> None:
    """A metric the cost joins on being absent is an error, never a silent $0."""
    record = _record()
    del record[metric]

    with pytest.raises(CostError, match=metric):
        price_result(record, Path("cell.json"), _inputs())


@pytest.mark.parametrize(
    "metric", ["duration", "total_input_tokens", "total_output_tokens"]
)
def test_null_metric_is_rejected(metric: str) -> None:
    """A metric present but null would price as 0 in bare arithmetic; reject it."""
    with pytest.raises(CostError, match=metric):
        price_result(_record(**{metric: None}), Path("cell.json"), _inputs())


def test_non_numeric_metric_is_rejected() -> None:
    """A metric present as a string is not the number the cost joins on."""
    with pytest.raises(CostError, match="total_input_tokens"):
        price_result(_record(total_input_tokens="lots"), Path("cell.json"), _inputs())


def test_boolean_metric_is_rejected() -> None:
    """A bool is an int subclass but never a valid token count or duration."""
    with pytest.raises(CostError, match="total_output_tokens"):
        price_result(_record(total_output_tokens=True), Path("cell.json"), _inputs())


def test_non_positive_duration_is_rejected() -> None:
    """A non-positive duration prices the whole run at $0; reject it."""
    with pytest.raises(CostError, match="non-positive duration"):
        price_result(_record(duration=0.0), Path("cell.json"), _inputs())


def test_zero_token_denominator_is_rejected() -> None:
    """Zero tokens on both sides is a zero denominator, not a $0 figure."""
    with pytest.raises(CostError, match="zero input and output"):
        price_result(
            _record(total_input_tokens=0, total_output_tokens=0),
            Path("cell.json"),
            _inputs(),
        )


@pytest.mark.parametrize("metric", ["total_input_tokens", "total_output_tokens"])
def test_negative_token_count_is_rejected(metric: str) -> None:
    """A negative token count is physically impossible and inverts the split."""
    with pytest.raises(CostError, match=metric):
        price_result(_record(**{metric: -1}), Path("cell.json"), _inputs())


@pytest.mark.parametrize("metric", ["duration", "total_input_tokens"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_metric_is_rejected(metric: str, bad: float) -> None:
    """NaN/Infinity slip past a bare < 0 or <= 0 check and price as nonsense."""
    with pytest.raises(CostError, match=metric):
        price_result(_record(**{metric: bad}), Path("cell.json"), _inputs())


def test_output_only_run_prices_a_finite_input_figure() -> None:
    """Zero input, positive output is a valid r*O denominator, not a rejection."""
    priced = price_result(
        _record(total_input_tokens=0, total_output_tokens=1_000_000),
        Path("cell.json"),
        _inputs(),
    )

    assert priced["cost_per_1m_input_usd"] == pytest.approx(2.0)


def test_token_counts_are_emitted_as_integers() -> None:
    """Token counts are counts, echoed as ints, not the float used internally."""
    priced = price_result(_record(), Path("cell.json"), _inputs())

    assert isinstance(priced["total_input_tokens"], int)
    assert isinstance(priced["total_output_tokens"], int)


@pytest.mark.parametrize("field", ["weight_checksum", "vllm_version", "quant_recipe"])
def test_empty_provenance_is_rejected(field: str) -> None:
    """A $/1M figure detached from its artifact is a lie; blank provenance fails."""
    pins = {**PINS, field: ""}
    with pytest.raises(CostError, match=field.replace("_", "-")):
        CostInputs(price_per_hour=2.0, output_input_ratio=1.0, **pins)
