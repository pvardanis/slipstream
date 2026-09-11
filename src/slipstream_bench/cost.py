"""Price a ``vllm bench serve`` result into $/1M input and output tokens.

Reads the token counts and wall-clock the harness saved (``--save-result``),
multiplies the wall-clock by the instance hourly price for the run's cost, then
splits that whole machine cost across input and output tokens by a pinned
output:input ratio into $/1M-input and $/1M-output reported separately (ratio 1
prices them equally; a commercial-style ratio like 3 weights decode tokens
heavier). The figure is only meaningful pinned to the artifact that produced
it, so the weight checksum, vLLM version, and quant recipe ride on every
record. The output is a pure function of its inputs — a re-run reproduces it.

Input token count I and output token count O, hourly price P, wall-clock D
seconds, ratio r: run cost C = P * D / 3600; per-input-token price
p_in = C / (I + r*O); then $/1M-input = p_in * 1e6 and $/1M-output = r * p_in *
1e6.
"""

from dataclasses import dataclass
from pathlib import Path

from slipstream_bench.results import numeric_metric, read_result

_SECONDS_PER_HOUR = 3600
_TOKENS_PER_MILLION = 1_000_000


class CostError(Exception):
    """A cost input that cannot produce a meaningful $/1M figure."""


@dataclass(frozen=True)
class CostInputs:
    """The price and provenance every priced record is pinned to.

    The ratio is required, not defaulted: leaving it to default 1 would silently
    price input and output equally and report the very blended figure the
    separation exists to avoid. Pinning the blend convention is a deliberate act,
    like the provenance pins.
    """

    price_per_hour: float
    output_input_ratio: float
    weight_checksum: str
    vllm_version: str
    quant_recipe: str

    def __post_init__(self) -> None:
        """Reject a non-positive price or ratio before any division.

        A zero price is a free-GPU fiction and a non-positive ratio inverts the
        input/output split, so both fail fast rather than emit a nonsense figure.

        :raise CostError: when the price or ratio is not strictly positive.
        """
        if self.price_per_hour <= 0:
            raise CostError(
                f"invalid price-per-hour {self.price_per_hour}: want a positive number"
            )
        if self.output_input_ratio <= 0:
            raise CostError(
                f"invalid output-input-ratio {self.output_input_ratio}: "
                f"want a positive number"
            )
        # A cost figure detached from what produced it is a lie waiting to happen:
        # refuse one without the full provenance triple pinned.
        provenance = (
            ("weight-checksum", self.weight_checksum),
            ("vllm-version", self.vllm_version),
            ("quant-recipe", self.quant_recipe),
        )
        for name, value in provenance:
            if not value:
                raise CostError(f"missing {name}: provenance must be pinned")


def price_result(record: dict, source: Path, inputs: CostInputs) -> dict:
    """Price one result record into a single cost record.

    :param record: the parsed ``vllm bench serve`` result.
    :param source: the file the record came from, echoed onto the cost record.
    :param inputs: the price and provenance the figure is pinned to.
    :return: the cost record: the split $/1M figures, the run cost, the echoed
        bench metrics, and the pinned provenance.
    :raise CostError: when a joined-on metric is missing or non-numeric, the
        duration is non-positive, or both token counts are zero (a zero
        denominator).
    """
    duration = numeric_metric(record, source, "duration", error_cls=CostError)
    input_tokens = numeric_metric(
        record, source, "total_input_tokens", error_cls=CostError
    )
    output_tokens = numeric_metric(
        record, source, "total_output_tokens", error_cls=CostError
    )

    # A non-positive duration prices the whole run at $0, and zero tokens on both
    # sides is a zero denominator — both are the silent-$0 the metrics guard.
    if duration <= 0:
        raise CostError(f"result {source} has non-positive duration (nothing to price)")
    if input_tokens + output_tokens <= 0:
        raise CostError(
            f"result {source} has zero input and output tokens (nothing to price)"
        )

    run_cost = inputs.price_per_hour * duration / _SECONDS_PER_HOUR
    price_in = run_cost / (input_tokens + inputs.output_input_ratio * output_tokens)
    return {
        "source": str(source),
        "model_id": record.get("model_id"),
        "duration_s": duration,
        "completed": record.get("completed"),
        "total_input_tokens": int(input_tokens),
        "total_output_tokens": int(output_tokens),
        "price_per_hour_usd": inputs.price_per_hour,
        "output_input_ratio": inputs.output_input_ratio,
        "run_cost_usd": run_cost,
        "cost_per_1m_input_usd": price_in * _TOKENS_PER_MILLION,
        "cost_per_1m_output_usd": (
            inputs.output_input_ratio * price_in * _TOKENS_PER_MILLION
        ),
        "weight_checksum": inputs.weight_checksum,
        "vllm_version": inputs.vllm_version,
        "quant_recipe": inputs.quant_recipe,
    }


def price_files(files: list[Path], inputs: CostInputs) -> list[dict]:
    """Price each result file into a cost record, order preserved.

    :param files: the result JSON files to price, in report order.
    :param inputs: the price and provenance the figures are pinned to.
    :return: one cost record per file, in the order given.
    :raise CostError: on a bad input file (see :func:`price_result`).
    :raise ResultError: when a file cannot be read (see
        :func:`slipstream_bench.results.read_result`).
    """
    return [price_result(read_result(file), file, inputs) for file in files]
