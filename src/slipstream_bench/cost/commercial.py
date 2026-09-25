"""Price a ``vllm bench serve`` result at a commercial API's published $/1M rates.

The commercial arm of the L0 baseline is fired at a per-token API, so it is
priced from the token counts the run measured (``--save-result``) times the
provider's quoted $/1M-input and $/1M-output rates — never a run wall-clock,
which a per-token API does not bill on, and never a figure quoted off a docs
page without the tokens being real. The model is a flat per-token list rate:
provider wrinkles like cached-input discounts, batch rates, or per-request
minimums are out of scope and would make this figure diverge from an invoice. The rates are the published list numbers;
what makes the record a baseline rather than a quote is that the token counts
come from the same workload the self-hosted arm ran, so the two $/1M figures
compare apples to apples. The quote is only meaningful pinned to who quoted it
and when, so the provider, model, and quote date ride on every record. The
tokenizer vLLM synthesised the workload with rides too: it is the ruler any
locally-counted tokens are on, and on a commercial run it is never the provider's.
vLLM's result JSON records no flag for whether a run's counts came from the
provider's ``usage`` block or a local retokenization fallback, so pinning this
ruler on every record does not by itself prove which source a figure used — it
makes the ruler explicit rather than absent, so a mismatch with the provider's is
at least visible instead of silently assumed away. The output is a pure function
of its inputs — a re-run reproduces it.

Input token count I, output token count O, input rate r_in and output rate
r_out in $/1M: run cost C = (I * r_in + O * r_out) / 1e6; the reported
$/1M-input and $/1M-output are r_in and r_out unchanged.
"""

from datetime import date, datetime
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict

from slipstream_bench.cost.config import load_provenance
from slipstream_bench.cost.fields import NonEmptyStr, PositiveFiniteFloat
from slipstream_bench.results import read_result, to_numeric_metric

_TOKENS_PER_MILLION = 1_000_000


class CommercialCostError(Exception):
    """A commercial-cost input that cannot produce a meaningful $/1M figure."""


def _coerce_yaml_date_to_iso(value: object) -> object:
    """Render an unquoted YAML date back to its ISO text before the format check.

    PyYAML reads an unquoted ``price_quoted_on: 2026-09-11`` as a ``datetime.date``.
    The record echoes the quote date as a string, so a parsed date is coerced to its
    ISO text; a quoted string passes through untouched for the validator to judge.

    Only a plain date is coerced. An unquoted ``2026-09-11 10:00:00`` parses to a
    ``datetime`` (a ``date`` subclass); it is left untouched so the ``str`` field
    rejects it rather than a bare timestamp being read as a quote day.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return value


def _require_iso_date(value: str) -> str:
    """Reject a quote date that is not an ISO date.

    A free-text date pins nothing reproducible; an ISO date is a real day the
    published rate can be checked against.

    :raise ValueError: when the value is not an ISO date like 2026-09-11.
    """
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(
            f"price_quoted_on '{value}' is not an ISO date like 2026-09-11"
        ) from error
    return value


# The quote date: an unquoted YAML date is coerced to its ISO text, then the string
# is required to parse as an ISO date so the quote provenance is a real day.
IsoDateStr = Annotated[
    str, BeforeValidator(_coerce_yaml_date_to_iso), AfterValidator(_require_iso_date)
]


class CommercialCostInputs(BaseModel):
    """The quoted rates and the provenance every priced record is pinned to.

    Authored in a per-command YAML file and validated once here at the boundary it
    crosses (ADR-0011): each rate must be positive and finite, the provider and
    model pins non-blank, and the quote date a real ISO day. Unknown keys are
    forbidden so a typo in the reviewed artifact fails loudly.

    The rates are the provider's published $/1M numbers; pinning who quoted them
    (``api``, ``model``) and when (``price_quoted_on``) is what keeps the figure
    honest, since a published rate drifts and a baseline must say which one it
    used.
    """

    # ``model`` is a provider model id, not a pydantic ``model_``-namespaced field,
    # so the protected namespace is cleared to name it plainly without a warning.
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    input_price_per_1m: PositiveFiniteFloat
    output_price_per_1m: PositiveFiniteFloat
    api: NonEmptyStr
    model: NonEmptyStr
    price_quoted_on: IsoDateStr


def load_commercial_cost_inputs(path: Path) -> CommercialCostInputs:
    """Read a run's commercial provenance YAML into :class:`CommercialCostInputs`.

    :param path: the provenance YAML file (api, model, quote date, $/1M rates).
    :return: the validated inputs.
    :raise CommercialCostError: on any read/parse/validate failure (see
        :func:`slipstream_bench.cost.config.load_provenance`).
    """
    return load_provenance(path, CommercialCostInputs, error_cls=CommercialCostError)


def _read_tokenizer_id(record: dict[str, object], source: Path) -> str:
    """Read the tokenizer vLLM synthesised the run's workload with.

    ``vllm bench serve`` writes ``tokenizer_id`` (``--tokenizer`` or, defaulted,
    the model id) into every ``--save-result`` file. It is the ruler any
    locally-counted tokens are on; on a commercial run it is never the provider's.
    vLLM records no flag for whether a run's counts came from the provider
    ``usage`` block or a local retokenization fallback, so this does not prove
    which source priced a figure — it pins the local ruler so a mismatch with the
    provider's is visible rather than absent. An unrecorded tokenizer erases even
    that, so it fails fast.

    :param record: the parsed result record.
    :param source: the file the record came from, for the error message.
    :return: the recorded tokenizer id.
    :raise CommercialCostError: when ``tokenizer_id`` is absent, null, non-string,
        or blank.
    """
    value = record.get("tokenizer_id")
    if not isinstance(value, str) or not value.strip():
        raise CommercialCostError(
            f"result {source} missing or blank tokenizer_id: the local tokenizer "
            f"this arm pins as provenance is the field it cannot omit"
        )
    return value


def price_commercial_result(
    record: dict[str, object], source: Path, inputs: CommercialCostInputs
) -> dict[str, object]:
    """Price one result record at the quoted rates into a single cost record.

    :param record: the parsed ``vllm bench serve`` result.
    :param source: the file the record came from, echoed onto the cost record.
    :param inputs: the quoted rates and the provenance the figure is pinned to.
    :return: the cost record: the reported $/1M rates, the run cost, the echoed
        token counts, the pinned local tokenizer, and the pinned quote provenance.
    :raise CommercialCostError: when a joined-on token count is missing or
        non-numeric, both token counts are zero (a $0 run with nothing to price),
        or the local tokenizer is unrecorded.
    """
    tokenizer_id = _read_tokenizer_id(record, source)

    input_tokens = to_numeric_metric(
        record, source, "total_input_tokens", error_cls=CommercialCostError
    )
    output_tokens = to_numeric_metric(
        record, source, "total_output_tokens", error_cls=CommercialCostError
    )

    # Zero tokens on both sides is a $0 run that priced nothing — the silent-$0
    # the metric guards exist to catch.
    if input_tokens + output_tokens <= 0:
        raise CommercialCostError(
            f"result {source} has zero input and output tokens (nothing to price)"
        )

    run_cost = (
        input_tokens * inputs.input_price_per_1m
        + output_tokens * inputs.output_price_per_1m
    ) / _TOKENS_PER_MILLION
    return {
        "source": str(source),
        "model_id": record.get("model_id"),
        "tokenizer_id": tokenizer_id,
        # The segment keys the baseline report joins the arms on (see report.py); a
        # raw result missing either echoes null, which no spine segment matches, so
        # the report fails the join rather than pricing it silently.
        "request_rate": record.get("request_rate"),
        "prefix_share": record.get("prefix_share"),
        "completed": record.get("completed"),
        "total_input_tokens": int(input_tokens),
        "total_output_tokens": int(output_tokens),
        "run_cost_usd": run_cost,
        "cost_per_1m_input_usd": inputs.input_price_per_1m,
        "cost_per_1m_output_usd": inputs.output_price_per_1m,
        "api": inputs.api,
        "model": inputs.model,
        "price_quoted_on": inputs.price_quoted_on,
    }


def price_commercial_files(
    files: list[Path], inputs: CommercialCostInputs
) -> list[dict[str, object]]:
    """Price each result file at the quoted rates, order preserved.

    :param files: the result JSON files to price, in report order.
    :param inputs: the quoted rates and the provenance the figures are pinned to.
    :return: one cost record per file, in the order given.
    :raise CommercialCostError: on a bad input file (see
        :func:`price_commercial_result`).
    :raise ResultError: when a file cannot be read (see
        :func:`slipstream_bench.results.read_result`).
    """
    return [price_commercial_result(read_result(file), file, inputs) for file in files]
