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

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from slipstream_bench.results import read_result, to_numeric_metric

_TOKENS_PER_MILLION = 1_000_000


class CommercialCostError(Exception):
    """A commercial-cost input that cannot produce a meaningful $/1M figure."""


@dataclass(frozen=True)
class CommercialCostInputs:
    """The quoted rates and the provenance every priced record is pinned to.

    The rates are the provider's published $/1M numbers; pinning who quoted them
    (``api``, ``model``) and when (``price_quoted_on``) is what keeps the figure
    honest, since a published rate drifts and a baseline must say which one it
    used.
    """

    input_price_per_1m: float
    output_price_per_1m: float
    api: str
    model: str
    price_quoted_on: str

    def __post_init__(self) -> None:
        """Reject a non-positive rate or unpinned quote provenance.

        A non-positive rate is a free-token fiction, and a rate detached from the
        provider, model, and date it was quoted at is a list price with no
        provenance — the very thing this arm exists to avoid — so both fail fast.

        :raise CommercialCostError: when a rate is not strictly positive, a
            provenance field is blank, or the quote date is not an ISO date.
        """
        rates = (
            ("input-price-per-1m", self.input_price_per_1m),
            ("output-price-per-1m", self.output_price_per_1m),
        )
        for name, value in rates:
            # NaN and Infinity slip past a bare <= 0 check (NaN <= 0 is False,
            # inf <= 0 is False) and would poison every run cost; reject them too.
            if not math.isfinite(value) or value <= 0:
                raise CommercialCostError(
                    f"invalid {name} {value}: want a positive, finite number"
                )
        provenance = (
            ("api", self.api),
            ("model", self.model),
            ("price-quoted-on", self.price_quoted_on),
        )
        for name, value in provenance:
            if not value:
                raise CommercialCostError(f"missing {name}: the quote must be pinned")
        # A free-text date pins nothing reproducible; require an ISO date so the
        # quote provenance is a real day the published rate can be checked against.
        try:
            date.fromisoformat(self.price_quoted_on)
        except ValueError as error:
            raise CommercialCostError(
                f"invalid price-quoted-on '{self.price_quoted_on}': "
                f"want an ISO date like 2026-09-11"
            ) from error


def _read_tokenizer_id(record: dict, source: Path) -> str:
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
            f"result {source} missing or blank tokenizer_id: the ruler the token "
            f"counts were measured on is the provenance this arm cannot omit"
        )
    return value


def price_commercial_result(
    record: dict, source: Path, inputs: CommercialCostInputs
) -> dict:
    """Price one result record at the quoted rates into a single cost record.

    :param record: the parsed ``vllm bench serve`` result.
    :param source: the file the record came from, echoed onto the cost record.
    :param inputs: the quoted rates and the provenance the figure is pinned to.
    :return: the cost record: the reported $/1M rates, the run cost, the echoed
        token counts, the count-source tokenizer, and the pinned quote provenance.
    :raise CommercialCostError: when a joined-on token count is missing or
        non-numeric, both token counts are zero (a $0 run with nothing to price),
        or the count-source tokenizer is unrecorded.
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
) -> list[dict]:
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
