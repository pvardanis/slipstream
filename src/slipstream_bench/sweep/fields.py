"""Annotated field types the sweep config and grid models both validate with.

One home for the range and uniqueness primitives so ``config.py`` and ``grid.py``
pin an out-of-range, repeated, or empty swept value the same way, from a single
definition rather than a copy in each model.
"""

import math
from typing import Annotated, TypeVar

from pydantic import AfterValidator, BeforeValidator, Field

T = TypeVar("T")

NonEmptyStr = Annotated[str, Field(min_length=1)]

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


def unique(values: list[T]) -> list[T]:
    """Reject a repeated swept value: two equal points collide on one results subdir."""
    if len(set(values)) != len(values):
        raise ValueError("swept values must be unique, none repeated")
    return values


def _coerce_to_str(value: object) -> object:
    """Render a bare YAML number as the string the vLLM ``--request-rate`` flag takes.

    ``request_rate: 8`` parses to an int and ``request_rate: .inf`` to a float; the
    flag is a string either way, so coerce here and let the author leave a plain
    number unquoted. A non-number passes through untouched for the validator to judge.
    """
    return str(value) if isinstance(value, (int, float)) else value


def _validate_request_rate(value: str) -> str:
    """Reject a request rate that is neither a non-negative number nor 'inf'.

    :raise ValueError: when the value is negative, non-finite, or not a number/'inf'.
    """
    if value == "inf":
        return value
    try:
        rate = float(value)
    except ValueError:
        raise ValueError(
            f"invalid request_rate '{value}': want a non-negative number or 'inf'"
        ) from None
    if not math.isfinite(rate) or rate < 0:
        raise ValueError(
            f"invalid request_rate '{value}': want a non-negative number or 'inf'"
        )
    return value


# A requests/sec rate or the literal 'inf'; a bare YAML number is coerced to the
# string the flag carries before the number/'inf' check runs.
RequestRate = Annotated[
    str, BeforeValidator(_coerce_to_str), AfterValidator(_validate_request_rate)
]

# A non-empty, no-duplicates sweep axis of positive ints (e.g. max-num-seqs).
UniquePositiveInts = Annotated[
    list[PositiveInt], Field(min_length=1), AfterValidator(unique)
]
# A non-empty, no-duplicates sweep axis of positive floats (e.g. burstiness).
UniquePositiveFloats = Annotated[
    list[Annotated[float, Field(gt=0)]], Field(min_length=1), AfterValidator(unique)
]
# Prefix-share percentages, each in 0..100, non-empty and no duplicates.
PrefixShares = Annotated[
    list[Annotated[int, Field(ge=0, le=100)]],
    Field(min_length=1),
    AfterValidator(unique),
]
# The closed-loop in-flight cap ladder; may be empty (open-loop), never repeats.
ConcurrencyLadder = Annotated[list[PositiveInt], AfterValidator(unique)]
# A non-empty SLO, each token a non-empty string (e.g. ttft:1000).
GoodputSlo = Annotated[list[NonEmptyStr], Field(min_length=1)]
