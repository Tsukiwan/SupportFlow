"""Money typing rules.

Amounts are ``Decimal`` with at most two decimal places. Binary floats are
rejected outright so money can never be represented imprecisely; callers
pass ``Decimal``, ``int`` or numeric strings instead.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator, Field


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("money must be Decimal, int or str — float is forbidden")
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(ge=0, max_digits=12, decimal_places=2),
]

PositiveMoney = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(gt=0, max_digits=12, decimal_places=2),
]
