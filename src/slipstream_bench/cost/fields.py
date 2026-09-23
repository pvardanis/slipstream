"""Annotated field types the cost provenance models validate prices and pins with.

One home for the positive-finite price and non-empty provenance primitives so the
self-hosted and commercial models pin a non-positive price or a blank provenance
field the same way, from a single definition rather than a copy in each model.
"""

from typing import Annotated

from pydantic import AfterValidator, Field


def _require_non_blank(value: str) -> str:
    """Reject a provenance pin that is blank once surrounding whitespace is stripped.

    ``Field(min_length=1)`` counts characters, so a whitespace-only pin would pass
    while describing no artifact; this mirrors the strip the result tokenizer pin is
    guarded with (:func:`slipstream_bench.cost.commercial._read_tokenizer_id`).

    :raise ValueError: when the value is blank after stripping.
    """
    if not value.strip():
        raise ValueError("provenance field must not be blank")
    return value


# A non-empty provenance string: a blank or whitespace-only pin describes no
# artifact and no quote, so both are rejected.
NonEmptyStr = Annotated[str, Field(min_length=1), AfterValidator(_require_non_blank)]

# A strictly positive, finite price or ratio. Zero is a free-resource fiction and a
# negative one inverts the split; NaN and Infinity slip past a bare <= 0 check and
# would poison every figure downstream, so allow_inf_nan bars them here.
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
