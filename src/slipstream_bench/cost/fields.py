"""Annotated field types the cost provenance models validate prices and pins with.

One home for the positive-finite price and non-empty provenance primitives so the
self-hosted and commercial models pin a non-positive price or a blank provenance
field the same way, from a single definition rather than a copy in each model.
"""

from typing import Annotated

from pydantic import Field

# A non-empty provenance string: a blank pin describes no artifact and no quote.
NonEmptyStr = Annotated[str, Field(min_length=1)]

# A strictly positive, finite price or ratio. Zero is a free-resource fiction and a
# negative one inverts the split; NaN and Infinity slip past a bare <= 0 check and
# would poison every figure downstream, so allow_inf_nan bars them here.
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
