"""Tests for the commercial-cost post-processor: measured tokens at a quoted rate.

The commercial arm is priced from the token counts the same ``vllm bench serve``
run measured, multiplied by the provider's published $/1M rates — never a run
wall-clock, which a per-token API does not bill on. Seeded from the same
checklist as the self-hosted cost tests: the split arithmetic, fractional
figures, order-preserving multi-file output, run-to-run reproducibility, the
pinned quote provenance, the count-source tokenizer echoed as provenance, and the
fail-fast guards on price, blank/ill-formed provenance, missing/null/non-numeric/
non-finite/negative token counts, a missing/blank tokenizer, and a zero-token
denominator. Unlike the self-hosted arm, no duration is read.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from slipstream_bench.cost.commercial import (
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
    tokenizer_id: object = "Qwen/Qwen2.5-0.5B-Instruct",
    **extra: object,
) -> dict:
    record = {
        "model_id": "gpt-4o-mini",
        "tokenizer_id": tokenizer_id,
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


def test_segment_keys_ride_on_every_record_for_the_report_join() -> None:
    """request_rate and prefix_share ride on so the report joins the arms by segment."""
    priced = price_commercial_result(
        _record(request_rate=8.0, prefix_share=90), Path("cell.json"), _inputs()
    )

    assert priced["request_rate"] == 8.0
    assert priced["prefix_share"] == 90


def test_absent_segment_keys_ride_as_null() -> None:
    """A raw result without our injected prefix_share echoes it as null."""
    priced = price_commercial_result(_record(), Path("cell.json"), _inputs())

    assert priced["request_rate"] is None
    assert priced["prefix_share"] is None


def test_tokenizer_id_rides_on_every_record_as_count_provenance() -> None:
    """The tokenizer the counts were measured on is echoed so a local ruler is detectable."""
    priced = price_commercial_result(_record(), Path("cell.json"), _inputs())

    assert priced["tokenizer_id"] == "Qwen/Qwen2.5-0.5B-Instruct"


def test_missing_tokenizer_id_is_rejected() -> None:
    """A record whose count-source tokenizer is unknown cannot be trusted; reject it."""
    record = _record()
    del record["tokenizer_id"]

    with pytest.raises(CommercialCostError, match="tokenizer_id"):
        price_commercial_result(record, Path("cell.json"), _inputs())


@pytest.mark.parametrize("bad", [None, "", "   ", 123])
def test_blank_or_non_string_tokenizer_id_is_rejected(bad: object) -> None:
    """A null, blank, or non-string tokenizer_id pins no real ruler; reject it."""
    with pytest.raises(CommercialCostError, match="tokenizer_id"):
        price_commercial_result(_record(tokenizer_id=bad), Path("cell.json"), _inputs())


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


_VALID_PAYLOAD = {"input_price_per_1m": 0.5, "output_price_per_1m": 1.5, **PINS}


def test_valid_payload_validates() -> None:
    """A complete, in-range payload validates into CommercialCostInputs."""
    inputs = CommercialCostInputs.model_validate(_VALID_PAYLOAD)

    assert inputs.input_price_per_1m == 0.5
    assert inputs.price_quoted_on == "2026-09-11"


def test_unquoted_iso_date_is_coerced_from_a_yaml_date() -> None:
    """An unquoted YAML date parses to a date object; the model keeps its ISO text."""
    from datetime import date

    inputs = CommercialCostInputs.model_validate(
        {**_VALID_PAYLOAD, "price_quoted_on": date(2026, 9, 11)}
    )

    assert inputs.price_quoted_on == "2026-09-11"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # A non-positive rate is a free-token fiction; NaN/Infinity slip past a bare
        # <= 0 check and poison every run cost, so the model rejects them too.
        ("input_price_per_1m", 0.0),
        ("input_price_per_1m", -1.0),
        ("input_price_per_1m", float("nan")),
        ("input_price_per_1m", float("inf")),
        ("output_price_per_1m", 0.0),
        ("output_price_per_1m", -1.0),
        ("output_price_per_1m", float("nan")),
        ("output_price_per_1m", float("inf")),
        # A quoted figure detached from provider/model/date is a list price with no
        # provenance; blank fields and a non-ISO quote date both fail.
        ("api", ""),
        ("model", ""),
        ("price_quoted_on", ""),
        ("price_quoted_on", "last tuesday"),
    ],
)
def test_rejection_matrix(field: str, value: object) -> None:
    """model_validate rejects a bad rate, blank pin, or non-ISO date, naming the field."""
    with pytest.raises(ValidationError, match=field):
        CommercialCostInputs.model_validate({**_VALID_PAYLOAD, field: value})


def test_extra_key_is_forbidden() -> None:
    """An unknown key is a typo in a reviewed artifact, not a silent pass-through."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CommercialCostInputs.model_validate({**_VALID_PAYLOAD, "unknown_knob": 1})


@pytest.mark.parametrize("missing", list(_VALID_PAYLOAD))
def test_missing_field_is_rejected(missing: str) -> None:
    """Every rate and provenance field is required; an omitted one fails."""
    payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != missing}
    with pytest.raises(ValidationError, match=missing):
        CommercialCostInputs.model_validate(payload)


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


def test_input_only_run_prices_the_input_side() -> None:
    """Positive input, zero output is a valid run, priced on the input rate alone."""
    priced = price_commercial_result(
        _record(total_input_tokens=1_000_000, total_output_tokens=0),
        Path("cell.json"),
        _inputs(),
    )

    assert priced["run_cost_usd"] == pytest.approx(0.5)
