"""Deterministic conversion from wire strings to domain values.

The extraction layer returns money and quantities exactly as printed. This is
where they become `Decimal`, and it is deliberately the only place that happens
-- so the rules are in one file, tested against both real number formats, and a
model is never asked to do arithmetic or formatting.

Every real shape this must handle, taken from the corpus:

    "$118936.42"     Fidelity: no thousands separators
    "$2,417.86"      Vanguard: thousands separators
    "+$44725.87"     signed gain
    "-$0.28"         signed loss
    "+60.27%"        signed percentage
    "1,348.207"      fractional share count
    "0"              a closed position, not a missing one
    "--" / "" / "N/A"  not reported -- becomes None, never Decimal("0")
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Final

#: Everything a source uses to mean "not reported". Distinct from zero.
NULL_SENTINELS: Final[frozenset[str]] = frozenset(
    {"", "--", "---", "n/a", "na", "none", "null", "-", "—", "–"}
)

#: Characters that decorate a number without changing it.
_STRIP_CHARS: Final[str] = "$,%  \t\r\n"

_PAREN_NEGATIVE: Final[re.Pattern[str]] = re.compile(r"^\((.*)\)$")

#: Accepted date shapes, in the order they are tried.
_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%b-%d-%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
)

_DATE_IN_TEXT: Final[re.Pattern[str]] = re.compile(
    r"(\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|[A-Z][a-z]{2}-\d{1,2}-\d{4}"
    r"|[A-Z][a-z]{2,8}\s+\d{1,2},\s*\d{4})"
)


class NormalizationError(ValueError):
    """A value was present but could not be understood.

    Deliberately distinct from "absent": absence is normal and becomes None,
    whereas an unparseable value is a real problem that must surface rather
    than silently become zero.
    """


def is_null_sentinel(raw: str | None) -> bool:
    if raw is None:
        return True
    return raw.strip().lower() in NULL_SENTINELS


def parse_decimal(raw: str | None, *, field: str = "value") -> Decimal | None:
    """Parse a printed number into a Decimal, or None when not reported.

    Never returns Decimal("0") for a missing value: a zero holding and an
    unreported one are different facts and both occur in the real data.
    """
    if is_null_sentinel(raw):
        return None
    assert raw is not None
    text = unicodedata.normalize("NFKC", raw).strip()

    # Accounting style: (1,234.56) means negative.
    negative = False
    paren = _PAREN_NEGATIVE.match(text)
    if paren:
        negative = True
        text = paren.group(1)

    text = text.strip()
    if text.startswith(("+", "−", "-")):
        if text[0] in "-−":
            negative = not negative
        text = text[1:]

    cleaned = "".join(ch for ch in text if ch not in _STRIP_CHARS)
    # A stray currency symbol or unicode minus can survive the strip set.
    cleaned = cleaned.replace("−", "").replace("£", "").replace("€", "")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise NormalizationError(f"cannot parse {field} from {raw!r}") from exc
    return -value if negative else value


def parse_money(raw: str | None, *, field: str = "amount") -> Decimal | None:
    """Parse a monetary amount. Alias of :func:`parse_decimal` for readability."""
    return parse_decimal(raw, field=field)


def parse_quantity(raw: str | None) -> Decimal | None:
    """Parse a share or unit count, preserving fractional shares exactly."""
    return parse_decimal(raw, field="quantity")


def parse_percent(raw: str | None) -> Decimal | None:
    """Parse a percentage as a *fraction*: '+60.27%' becomes 0.6027."""
    value = parse_decimal(raw, field="percent")
    return None if value is None else value / Decimal(100)


def parse_date(raw: str | None) -> dt.date | None:
    """Parse a date from a printed string, including one embedded in prose.

    Handles the shapes the corpus actually contains, e.g.
    'Aug-26-2026', '08/25/2026', 'Date downloaded Aug-26-2026 3:04 a.m ET' and
    'Value as of: August 25, 2026, 4:15 p.m., Eastern time'.
    """
    if is_null_sentinel(raw):
        return None
    assert raw is not None
    text = unicodedata.normalize("NFKC", raw).strip().strip('"')

    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    match = _DATE_IN_TEXT.search(text)
    if match:
        found = match.group(1)
        for fmt in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(found, fmt).date()
            except ValueError:
                continue
    raise NormalizationError(f"cannot parse a date from {raw!r}")


_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_PUNCT: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9 ]+")

#: Suffixes that describe share class or wrapper, not a different instrument.
_NOISE_WORDS: Final[frozenset[str]] = frozenset(
    {"etf", "fund", "inc", "corp", "the", "class", "usd", "shares", "trust"}
)


def normalize_security_key(symbol: str | None, description: str) -> str:
    """Identity key for a security.

    The ticker when there is one; otherwise a normalized form of the
    description, because 12 of 86 real holdings have no ticker at all and must
    still be identifiable across snapshots.
    """
    if symbol and symbol.strip() and not is_null_sentinel(symbol):
        return symbol.strip().upper()

    text = unicodedata.normalize("NFKD", description or "").lower()
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in _WHITESPACE.split(text) if t and t not in _NOISE_WORDS]
    if not tokens:
        raise NormalizationError("a holding has neither a symbol nor a usable description")
    return "-".join(tokens)


def mask_account_key(key: str) -> str:
    """Mask an account identifier for display and logging.

    Keeps enough to be recognisable, never enough to be reused. Applied to
    every account key that reaches the API or a log line.
    """
    cleaned = (key or "").strip()
    if not cleaned:
        return "unknown"
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    # Cap the stars: masking a 36-character UUID one-for-one produces a wall of
    # asterisks that is harder to read than the four characters that identify it.
    stars = min(len(cleaned) - 4, 8)
    return f"{'*' * stars}{cleaned[-4:]}"
