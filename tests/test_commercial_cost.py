"""Tests for the commercial-cost post-processor: measured tokens at a quoted rate.

The commercial arm is priced from the token counts the same ``vllm bench serve``
run measured, multiplied by the provider's published $/1M rates — never a run
wall-clock, which a per-token API does not bill on. Seeded from the same
checklist as the self-hosted cost tests: the split arithmetic, fractional
figures, order-preserving multi-file output, run-to-run reproducibility, the
pinned quote provenance, and the fail-fast guards on price, blank/ill-formed
provenance, missing/null/non-numeric/non-finite/negative token counts, and a
zero-token denominator. Unlike the self-hosted arm, no duration is read.
"""

import json
from pathlib import Path

import pytest

from slipstream_bench.commercial_cost import (
    CommercialCostError,
    CommercialCostInputs,
    price_commercial_files,
    price_commercial_result,
)

PINS = {
    "api": "openai",
    "model": "gpt-4o-mini",
    "price_quoted_on": "2026-09-11",
}


def _inputs(
    *, input_price_per_1m: float = 0.5, output_price_per_1m: float = 1.5
) -> CommercialCostInputs:
    return CommercialCostInputs(
        input_price_per_1m=input_price_per_1m,
        output_price_per_1m=output_price_per_1m,
        **PINS,
    )


def _record(
    *,
    total_input_tokens: object = 1_000_000,
    total_output_tokens: object = 1_000_000,
    **extra: object,
) -> dict:
    record = {
        "model_id": "gpt-4o-mini",
        "completed": 100,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }
    record.update(extra)
    return record


def test_run_cost_is_tokens_times_quoted_rate() -> None:
    """1M in @ $0.50 + 1M out @ $1.50 -> $2.00 run cost; rates echoed as $/1M."""
    priced = price_commercial_result(_record(), Path("cell.json"), _inputs())

    assert priced["run_cost_usd"] == pytest.approx(2.0)
    assert priced["cost_per_1m_input_usd"] == pytest.approx(0.5)
    assert priced["cost_per_1m_output_usd"] == pytest.approx(1.5)


def test_fractional_token_counts_price_exactly() -> None:
    """250k in @ $3/1M + 500k out @ $6/1M -> $0.75 + $3.00 = $3.75."""
    priced = price_commercial_result(
        _record(total_input_tokens=250_000, total_output_tokens=500_000),
        Path("frac.json"),
        _inputs(input_price_per_1m=3.0, output_price_per_1m=6.0),
    )

    assert priced["run_cost_usd"] == pytest.approx(3.75)


def test_no_duration_is_needed_to_price_the_commercial_arm() -> None:
    """A per-token API is priced without a run wall-clock; a duration-less record prices."""
    record = _record()
    assert "duration" not in record

    priced = price_commercial_result(record, Path("cell.json"), _inputs())

    assert priced["run_cost_usd"] == pytest.approx(2.0)


def test_quote_provenance_and_bench_metrics_ride_on_every_record() -> None:
    """The quoted provider/model/date and echoed token counts are self-describing."""
    priced = price_commercial_result(_record(), Path("cell.json"), _inputs())

    assert priced["api"] == "openai"
    assert priced["model"] == "gpt-4o-mini"
    assert priced["price_quoted_on"] == "2026-09-11"
    assert priced["model_id"] == "gpt-4o-mini"
    assert priced["total_input_tokens"] == 1_000_000
    assert priced["source"] == "cell.json"


def test_token_counts_are_emitted_as_integers() -> None:
    """Token counts are counts, echoed as ints, not the float used internally."""
    priced = price_commercial_result(_record(), Path("cell.json"), _inputs())

    assert isinstance(priced["total_input_tokens"], int)
    assert isinstance(priced["total_output_tokens"], int)


def test_price_files_emits_one_record_per_file_in_order(tmp_path: Path) -> None:
    """A cost record per input file, in the order given."""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps(_record()))
    second.write_text(json.dumps(_record(total_output_tokens=2_000_000)))

    records = price_commercial_files([first, second], _inputs())

    assert [r["source"] for r in records] == [str(first), str(second)]


def test_same_input_reproduces_identical_output(tmp_path: Path) -> None:
    """A re-run reproduces the figure byte for byte."""
    result = tmp_path / "cell.json"
    result.write_text(json.dumps(_record()))

    run_a = json.dumps(price_commercial_files([result], _inputs()))
    run_b = json.dumps(price_commercial_files([result], _inputs()))

    assert run_a == run_b


def test_non_positive_input_price_is_rejected() -> None:
    """A non-positive input rate is a free-token fiction; reject it."""
    with pytest.raises(CommercialCostError, match="input-price-per-1m"):
        _inputs(input_price_per_1m=0.0)


def test_non_positive_output_price_is_rejected() -> None:
    """A non-positive output rate is a free-token fiction; reject it."""
    with pytest.raises(CommercialCostError, match="output-price-per-1m"):
        _inputs(output_price_per_1m=-1.0)


@pytest.mark.parametrize("field", ["api", "model", "price_quoted_on"])
def test_empty_quote_provenance_is_rejected(field: str) -> None:
    """A quoted figure detached from provider/model/date is a lie; blank fails."""
    pins = {**PINS, field: ""}
    with pytest.raises(CommercialCostError, match=field.replace("_", "-")):
        CommercialCostInputs(input_price_per_1m=0.5, output_price_per_1m=1.5, **pins)


def test_ill_formed_quote_date_is_rejected() -> None:
    """A price-quoted-on that is not an ISO date pins nothing real; reject it."""
    pins = {**PINS, "price_quoted_on": "last tuesday"}
    with pytest.raises(CommercialCostError, match="price-quoted-on"):
        CommercialCostInputs(input_price_per_1m=0.5, output_price_per_1m=1.5, **pins)


@pytest.mark.parametrize("metric", ["total_input_tokens", "total_output_tokens"])
def test_missing_metric_is_rejected(metric: str) -> None:
    """A token count the cost joins on being absent is an error, never a silent $0."""
    record = _record()
    del record[metric]

    with pytest.raises(CommercialCostError, match=metric):
        price_commercial_result(record, Path("cell.json"), _inputs())


@pytest.mark.parametrize("metric", ["total_input_tokens", "total_output_tokens"])
def test_null_metric_is_rejected(metric: str) -> None:
    """A token count present but null would price as 0 in bare arithmetic; reject it."""
    with pytest.raises(CommercialCostError, match=metric):
        price_commercial_result(_record(**{metric: None}), Path("cell.json"), _inputs())


def test_non_numeric_metric_is_rejected() -> None:
    """A token count present as a string is not the number the cost joins on."""
    with pytest.raises(CommercialCostError, match="total_input_tokens"):
        price_commercial_result(
            _record(total_input_tokens="lots"), Path("cell.json"), _inputs()
        )


def test_boolean_metric_is_rejected() -> None:
    """A bool is an int subclass but never a valid token count."""
    with pytest.raises(CommercialCostError, match="total_output_tokens"):
        price_commercial_result(
            _record(total_output_tokens=True), Path("cell.json"), _inputs()
        )


@pytest.mark.parametrize("metric", ["total_input_tokens", "total_output_tokens"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_metric_is_rejected(metric: str, bad: float) -> None:
    """NaN/Infinity slip past a bare < 0 check and price as nonsense; reject them."""
    with pytest.raises(CommercialCostError, match=metric):
        price_commercial_result(_record(**{metric: bad}), Path("cell.json"), _inputs())


@pytest.mark.parametrize("metric", ["total_input_tokens", "total_output_tokens"])
def test_negative_token_count_is_rejected(metric: str) -> None:
    """A negative token count is physically impossible; reject it."""
    with pytest.raises(CommercialCostError, match=metric):
        price_commercial_result(_record(**{metric: -1}), Path("cell.json"), _inputs())


def test_zero_token_denominator_is_rejected() -> None:
    """Zero tokens on both sides is a $0 run with nothing to price; reject it."""
    with pytest.raises(CommercialCostError, match="zero input and output"):
        price_commercial_result(
            _record(total_input_tokens=0, total_output_tokens=0),
            Path("cell.json"),
            _inputs(),
        )


def test_output_only_run_prices_the_output_side() -> None:
    """Zero input, positive output is a valid run, priced on the output rate alone."""
    priced = price_commercial_result(
        _record(total_input_tokens=0, total_output_tokens=1_000_000),
        Path("cell.json"),
        _inputs(),
    )

    assert priced["run_cost_usd"] == pytest.approx(1.5)
