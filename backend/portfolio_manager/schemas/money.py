"""Money on the wire.

Every monetary and quantity field leaves the API as a **string**, never a JSON
number. A JSON number is a float in most clients -- including JavaScript, where
`0.1 + 0.2 !== 0.3` -- so serialising $2,046,812.11 as a number invites a
rounding error that no amount of `Decimal` discipline on our side can prevent.

The frontend formats from the string. It never does arithmetic on money; the
server has already done it exactly.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema

#: Six decimal places on a ratio is one ten-thousandth of a percent -- far more
#: resolution than any display needs, and far less noise than the 28 significant
#: digits Decimal division produces by default. The analytics stay exact; only
#: the wire representation is trimmed.
_RATIO_PLACES = Decimal("0.000001")


def _format_ratio(value: Decimal) -> str:
    return format(value.quantize(_RATIO_PLACES, rounding=ROUND_HALF_UP).normalize(), "f")


def _format(value: Decimal) -> str:
    """Exact decimal text with no trailing-zero noise.

    The database stores NUMERIC(20,6), so $2,046,812.11 arrives as
    `2046812.110000`. Normalising first keeps the value exact while dropping the
    padding, which matters because clients compare these as strings.
    `normalize()` alone would render 100 as `1E+2`, hence the explicit `f`.
    """
    return format(value.normalize(), "f")


#: A required monetary amount, serialised as a string.
#:
#: The explicit `WithJsonSchema` matters as much as the serializer: without it
#: Pydantic derives the schema from the *input* type and publishes `number`,
#: so the OpenAPI contract would promise clients a float even though the wire
#: carries a string. A generated client would then parse it as one.
Money = Annotated[
    Decimal,
    PlainSerializer(_format, return_type=str, when_used="always"),
    WithJsonSchema({"type": "string", "description": "Exact decimal amount"}, mode="serialization"),
]

#: An optional monetary amount. `None` stays `null` -- distinct from "0.00",
#: because an unreported value and a zero value are different facts.
OptMoney = Annotated[
    Decimal | None,
    PlainSerializer(
        lambda v: None if v is None else _format(v),
        return_type=str | None,
        when_used="always",
    ),
    WithJsonSchema(
        {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Exact decimal amount, or null when not reported",
        },
        mode="serialization",
    ),
]

#: A ratio in [0, 1]. Also a string: a percentage of a large portfolio carries
#: enough significant digits to matter.
Ratio = Annotated[
    Decimal,
    PlainSerializer(_format_ratio, return_type=str, when_used="always"),
    WithJsonSchema({"type": "string", "description": "Ratio in [0, 1]"}, mode="serialization"),
]
