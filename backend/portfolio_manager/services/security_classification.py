"""Inferring what kind of instrument a holding is.

Asset allocation is meaningless without this, but a wrong classification is
worse than an absent one: it moves money between buckets in a chart the user
makes decisions from. So the rules below only fire on strong evidence, and
anything else stays `unknown` and is reported as such by the allocation
analytic rather than quietly folded into a real bucket.

Fund families name their products descriptively, which makes name matching far
more reliable here than it would be in most domains -- but the rules are still
ordered most-specific-first, because "Total International Stock Index" contains
both "international" and "stock".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ..models.enums import AssetClass, SecurityType


@dataclass(frozen=True)
class SecurityClassification:
    security_type: SecurityType
    asset_class: AssetClass
    confidence: Decimal
    rationale: str

    @property
    def is_known(self) -> bool:
        return self.asset_class is not AssetClass.UNKNOWN


_UNKNOWN = SecurityClassification(
    SecurityType.UNKNOWN, AssetClass.UNKNOWN, Decimal("0"), "no recognisable signal"
)

#: (pattern, security type, asset class, confidence, why). Order matters.
_RULES: tuple[tuple[re.Pattern[str], SecurityType, AssetClass, str, str], ...] = (
    (
        re.compile(r"money\s*mark|mmkt|treasury\s+only|cash\s+reserv|settlement"),
        SecurityType.MONEY_MARKET,
        AssetClass.CASH_EQUIVALENT,
        "0.95",
        "a money-market or settlement fund is a cash equivalent",
    ),
    (
        re.compile(r"\bt-?bill|treasury\s+bill|united\s+states\s+treasury|\bus\s+treasury"),
        SecurityType.BOND,
        AssetClass.CASH_EQUIVALENT,
        "0.9",
        "treasury bills are short-dated and treated as cash equivalents",
    ),
    (
        re.compile(r"real\s*estate|\breit\b"),
        SecurityType.ETF,
        AssetClass.REAL_ESTATE,
        "0.9",
        "a real-estate fund",
    ),
    (
        re.compile(
            r"internationa|internatl|\bintl\b|\bex[- ]?us\b|all\s*world|emerging|"
            r"\bemr\s*mkt|developed\s+mark|pacific|europe|\bftse\b"
        ),
        SecurityType.ETF,
        AssetClass.INTL_EQUITY,
        "0.9",
        "an international equity fund",
    ),
    (
        re.compile(
            r"\bbond\b|fixed\s+income|aggregate|\btips\b|inflation[- ]protect|municipal|"
            r"tax[- ]exempt|invest(ment)?[- ]grade|corporate\s+bond|short[- ]term\s+invest|"
            r"\b\d+\s*mo\s+tr\b"
        ),
        SecurityType.ETF,
        AssetClass.FIXED_INCOME,
        "0.9",
        "a bond or fixed-income fund",
    ),
    (
        re.compile(r"target\s+(date|retirement)|lifestrategy|\b20[2-7]\d\s+fund"),
        SecurityType.TARGET_DATE,
        AssetClass.MULTI_ASSET,
        "0.9",
        "a target-date or balanced fund holds several asset classes",
    ),
    (
        re.compile(r"commodit|\bgold\b|precious\s+metal"),
        SecurityType.ETF,
        AssetClass.COMMODITY,
        "0.85",
        "a commodity fund",
    ),
    (
        re.compile(
            r"s\s*&\s*p\s*500|total\s+stock|500\s+index|growth\s+index|value\s+index|"
            r"dividend|\bdiv\s+(app|yld)|high\s+div|small[- ]?cap|mid[- ]?cap|large[- ]?cap|"
            r"health\s*care|info(rmation)?\s+tech|semiconductor|\bphlx\b|energy|sector|"
            r"specialized|whitehall"
        ),
        SecurityType.ETF,
        AssetClass.US_EQUITY,
        "0.85",
        "a US equity index or sector fund",
    ),
)

#: Symbols whose name gives nothing away but whose identity is unambiguous.
_SYMBOL_HINTS: dict[str, tuple[SecurityType, AssetClass]] = {
    "SPY": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VOO": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VTI": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VTV": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VB": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VO": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VGT": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VHT": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VDE": (SecurityType.ETF, AssetClass.US_EQUITY),
    "SCHD": (SecurityType.ETF, AssetClass.US_EQUITY),
    "SOXX": (SecurityType.ETF, AssetClass.US_EQUITY),
    "VXUS": (SecurityType.ETF, AssetClass.INTL_EQUITY),
    "VEU": (SecurityType.ETF, AssetClass.INTL_EQUITY),
    "VPL": (SecurityType.ETF, AssetClass.INTL_EQUITY),
    "VIGI": (SecurityType.ETF, AssetClass.INTL_EQUITY),
    "BND": (SecurityType.ETF, AssetClass.FIXED_INCOME),
    "BIV": (SecurityType.ETF, AssetClass.FIXED_INCOME),
    "VCSH": (SecurityType.ETF, AssetClass.FIXED_INCOME),
    "VTIP": (SecurityType.ETF, AssetClass.FIXED_INCOME),
    "VNQ": (SecurityType.ETF, AssetClass.REAL_ESTATE),
    # Money-market funds whose names arrive truncated in real exports.
    "VUSXX": (SecurityType.MONEY_MARKET, AssetClass.CASH_EQUIVALENT),
    "VMFXX": (SecurityType.MONEY_MARKET, AssetClass.CASH_EQUIVALENT),
    "FZDXX": (SecurityType.MONEY_MARKET, AssetClass.CASH_EQUIVALENT),
    "FDLXX": (SecurityType.MONEY_MARKET, AssetClass.CASH_EQUIVALENT),
}

#: A company suffix anywhere in the name, not anchored to the end: real
#: holdings carry trailing share-class noise ("CORP COM", "INC COM CL A").
_EQUITY_NAME = re.compile(r"\b(inc|corp|corporation|company|plc|ltd|holdings|group)\b", re.I)

#: Vocabulary that means "this is a pooled vehicle, not a company". A name
#: containing any of it is never treated as an individual stock.
#:
#: This is the negative half of the equity test, and it exists because real
#: documents truncate: Vanguard reports Costco as "COSTCO WHOLESALE…", cutting
#: off the "CORP" the positive test relied on. Requiring a company suffix
#: therefore misses real holdings, while requiring the *absence* of fund
#: vocabulary is robust to truncation — a truncated fund name still carries its
#: issuer or its product word.
_FUND_VOCABULARY = re.compile(
    r"\b(fund|funds|index|etf|portfolio|trust|shares|idx|port|"
    r"vanguard|fidelity|ishares|spdr|invesco|schwab|state\s+street|"
    r"treasury|bond|money|market|admiral|institutional)\b",
    re.I,
)


def classify_security(symbol: str | None, name: str) -> SecurityClassification:
    """Infer instrument type and asset class, or decline."""
    ticker = (symbol or "").strip().upper()
    haystack = f"{name or ''}".lower()

    if ticker in _SYMBOL_HINTS:
        security_type, asset_class = _SYMBOL_HINTS[ticker]
        return SecurityClassification(
            security_type, asset_class, Decimal("0.95"), f"{ticker} is a well-known fund"
        )

    for pattern, security_type, asset_class, confidence, rationale in _RULES:
        if pattern.search(haystack):
            return SecurityClassification(
                security_type, asset_class, Decimal(confidence), rationale
            )

    # An individual stock: a short ticker, a company-shaped name, and nothing
    # that says "pooled vehicle". The company suffix is sufficient but not
    # necessary, because documents truncate it away.
    # A name too short to be a company name is a placeholder, not evidence.
    # One real export carries rows with the symbol "**" and a blank description.
    named = len((name or "").strip()) >= 3
    has_suffix = bool(_EQUITY_NAME.search(name or ""))
    no_fund_words = not _FUND_VOCABULARY.search(name or "")

    if ticker and ticker.isalpha() and 1 <= len(ticker) <= 5 and named:
        if has_suffix:
            return SecurityClassification(
                SecurityType.EQUITY,
                AssetClass.US_EQUITY,
                Decimal("0.75"),
                "a ticker with a company suffix in its name is an individual stock",
            )
        if no_fund_words:
            # Weaker evidence, and honestly scored: this infers from the
            # ABSENCE of fund vocabulary rather than the presence of a company
            # suffix. It exists because documents truncate the suffix away —
            # Vanguard reports Costco as "COSTCO WHOLESALE…". The risk it
            # carries is a non-US or non-equity instrument with a plain name,
            # which is why the confidence is low enough to be worth reviewing.
            return SecurityClassification(
                SecurityType.EQUITY,
                AssetClass.US_EQUITY,
                Decimal("0.6"),
                "a short ticker whose name contains no fund vocabulary is most "
                "likely an individual stock, though the name is truncated or terse",
            )

    return _UNKNOWN
