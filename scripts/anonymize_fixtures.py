#!/usr/bin/env python3
"""Derive committable test fixtures from a private corpus of real statements.

The real files in `test/` contain names, account numbers and balances, and are
gitignored. The test suite still needs data with the same awkward *shape*, so
this script produces synthetic equivalents that preserve every structural quirk
while carrying none of the content.

Two design rules make it safe:

* **The mapping is derived at runtime, never written down.** A script that
  contained the real names in order to replace them would itself leak them the
  moment it was committed.
* **Replacement is default-on, whitelist-off.** Every alphabetic token is
  pseudonymised unless it appears in a vocabulary of finance and structural
  words that the tests actually depend on ("ROTH", "ROLLOVER", "529"). Erring
  this way means an unrecognised surname is replaced rather than preserved.

Amounts are scaled by a fixed factor so magnitudes are meaningless, applied to
quantity and value but not price -- which keeps `quantity x price = value`
consistent, and preserves the real rounding drift proportionally.

Usage:
    python scripts/anonymize_fixtures.py [--source test/Fidelity.csv]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "test" / "Fidelity.csv"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "extraction"
OUTPUT = FIXTURES / "fidelity" / "fullview_anonymized.csv"
#: A canonical extraction of a direct-source document covering some of the same
#: accounts, for the precedence tests. Emitted as JSON rather than as a rendered
#: PDF: what the tests exercise is the pipeline downstream of extraction, and a
#: synthetic PDF would only test our own renderer.
PAIR_OUTPUT = FIXTURES / "vanguard" / "direct_source_anonymized.json"

#: Scales every amount. Chosen to be non-round so totals look unremarkable.
AMOUNT_FACTOR = Decimal("0.4137")

#: Words the tests depend on. Everything else is replaced.
PRESERVE = {
    # account structure and classification
    "ROTH",
    "IRA",
    "ROLLOVER",
    "TRADITIONAL",
    "BROKERAGE",
    "ACCOUNT",
    "ACCOUNTS",
    "JOINT",
    "WROS",
    "TOD",
    "CORPORATION",
    "CORPORATE",
    "INDIVIDUAL",
    "COLLEGE",
    "SAVINGS",
    "PLAN",
    "EQUITY",
    "TRUST",
    "HSA",
    "MARGIN",
    "CASH",
    "INC",
    # instrument vocabulary
    "ETF",
    "FUND",
    "FUNDS",
    "INDEX",
    "PORTFOLIO",
    "BOND",
    "STOCK",
    "MARKET",
    "MONEY",
    "MMKT",
    "TREASURY",
    "PREMIUM",
    "CLASS",
    "TOTAL",
    "INTERNATIONAL",
    "GROWTH",
    "VALUE",
    "HEALTH",
    "CARE",
    "TECH",
    "INFORMATION",
    "WORLD",
    "USD",
    "SHORT",
    "TERM",
    "INTEREST",
    "ACCUMULATION",
    "APPRECIATION",
    "DIVIDEND",
    "SMALL",
    "MID",
    "LARGE",
    "CAP",
    "REIT",
    "ENERGY",
    "SECTOR",
    "SP",
    "US",
    "INSTL",
    "TR",
    "BI",
    "MO",
    "FTSE",
    "ALL",
    "EX",
    "S",
    "P",
    # column headers and boilerplate
    "SYMBOL",
    "DESCRIPTION",
    "QUANTITY",
    "LAST",
    "PRICE",
    "CHANGE",
    "CURRENT",
    "TODAY",
    "GAIN",
    "LOSS",
    "DOLLAR",
    "PERCENT",
    "OF",
    "COST",
    "BASIS",
    "AVERAGE",
    "TYPE",
    "NUMBER",
    "NAME",
    "DATE",
    "DOWNLOADED",
    "ET",
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
    "A",
    "M",
    "AM",
    "PM",
}

#: Pseudonym pools. Deliberately unmistakable as fictional.
_GIVEN = (
    "Pat",
    "Alex",
    "Robin",
    "Sam",
    "Quinn",
    "Jordan",
    "Casey",
    "Riley",
    "Morgan",
    "Avery",
    "Rowan",
    "Skyler",
    "Emerson",
    "Finley",
)
_FAMILY = (
    "Sample",
    "Placeholder",
    "Fixture",
    "Testcase",
    "Synthetic",
    "Example",
    "Stand-in",
    "Dummy",
)
_OTHER = ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta")

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_LONG_DIGITS = re.compile(r"\b\d{6,}\b")
_ACCOUNT_CODE = re.compile(r"\b[A-Z]\d{6,}\b")
_HEX_ONLY = re.compile(r"[0-9a-fA-F]+")


def _pick(pool: tuple[str, ...], seed: str) -> str:
    digest = hashlib.sha256(seed.encode()).digest()
    return pool[digest[0] % len(pool)]


class Anonymizer:
    """Stable, seeded replacement of identities. Never stores originals."""

    def __init__(self) -> None:
        self._words: dict[str, str] = {}
        self._uuids: dict[str, str] = {}
        self._numbers: dict[str, str] = {}
        self._codes: dict[str, str] = {}
        self.replaced_tokens = 0

    def word(self, token: str) -> str:
        upper = token.upper().strip("'’-")
        if upper in PRESERVE or len(upper) <= 1:
            return token
        if token not in self._words:
            # Given names read as given names, so the fixture stays readable.
            pool = _GIVEN if len(token) <= 7 else _FAMILY
            base = _pick(pool + _OTHER, token)
            candidate, n = base, 1
            while candidate in self._words.values():
                n += 1
                candidate = f"{base}{n}"
            self._words[token] = candidate
        self.replaced_tokens += 1
        return self._words[token]

    def uuid(self, value: str) -> str:
        if value not in self._uuids:
            digest = hashlib.sha256(f"uuid:{value}".encode()).hexdigest()
            self._uuids[value] = (
                f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
            )
        return self._uuids[value]

    def number(self, value: str) -> str:
        """Replace a long digit run, preserving its length.

        Length matters: the tests rely on a six-plus-digit run being treated as
        an account identifier, and on two accounts sharing a base number that
        differs only by suffix.
        """
        if value not in self._numbers:
            digest = hashlib.sha256(f"num:{value}".encode()).hexdigest()
            digits = "".join(str(int(c, 16) % 10) for c in digest)[: len(value)]
            self._numbers[value] = digits
        return self._numbers[value]

    def code(self, value: str) -> str:
        if value not in self._codes:
            self._codes[value] = value[0] + self.number(value[1:])
        return self._codes[value]

    def text(self, value: str) -> str:
        """Replace identities, protecting each substitution from the next.

        Order matters and the passes overlap: a substituted UUID is full of
        hex letters that the word pass would happily rewrite, turning a valid
        UUID into gibberish and destroying the native-vs-aggregated account
        distinction the fixtures exist to exercise. So each identifier pass
        parks its result behind a placeholder containing no letters, and the
        placeholders are restored at the end.
        """
        parked: list[str] = []

        def park(replacement: str) -> str:
            parked.append(replacement)
            return f"\x00{len(parked) - 1}\x00"

        out = _UUID.sub(lambda m: park(self.uuid(m.group(0))), value)
        out = _ACCOUNT_CODE.sub(lambda m: park(self.code(m.group(0))), out)
        out = _LONG_DIGITS.sub(lambda m: park(self.number(m.group(0))), out)
        out = _TOKEN.sub(lambda m: self.word(m.group(0)), out)
        for index, replacement in enumerate(parked):
            out = out.replace(f"\x00{index}\x00", replacement)
        return out


def _scale(raw: str, places: str) -> str:
    """Scale a printed amount, preserving its exact formatting conventions."""
    text = raw.strip()
    if not text or text in {"--", "N/A"}:
        return raw
    sign = ""
    body = text
    if body[0] in "+-":
        sign, body = body[0], body[1:]
    currency = ""
    if body.startswith("$"):
        currency, body = "$", body[1:]
    if body.endswith("%"):
        return raw  # percentages are ratios; scaling would break them
    had_commas = "," in body
    try:
        value = Decimal(body.replace(",", ""))
    except InvalidOperation:
        return raw
    if value == 0:
        # Nothing to anonymise about zero, and the real file writes a bare "0"
        # for closed holdings. Reformatting it to "0.000" would make the
        # fixture subtly less faithful than the corpus it stands in for.
        return raw
    scaled = (value * AMOUNT_FACTOR).quantize(Decimal(places), rounding=ROUND_HALF_UP)
    formatted = f"{scaled:,.{len(places.split('.')[-1])}f}" if had_commas else str(scaled)
    return f"{sign}{currency}{formatted}"


def anonymize_csv(text: str) -> tuple[str, dict[str, int]]:
    """Anonymize a positions export, preserving every structural quirk."""
    anon = Anonymizer()
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SystemExit("source file is empty")

    header = rows[0]
    width = len(header)
    lowered = [h.strip().lstrip("﻿").lower() for h in header]

    def column(*names: str) -> int | None:
        for name in names:
            if name in lowered:
                return lowered.index(name)
        return None

    account_col = column("account number") or 0
    name_col = column("account name")
    quantity_col = column("quantity")
    value_cols = [
        i
        for i, h in enumerate(lowered)
        if any(k in h for k in ("value", "gain/loss dollar", "cost basis")) and "percent" not in h
    ]

    out_rows: list[list[str]] = [header]
    stats = {"data_rows": 0, "trailing_rows": 0}

    for row in rows[1:]:
        if len(row) >= width and row and row[0].strip():
            new = list(row)
            new[account_col] = anon.text(row[account_col])
            if name_col is not None:
                new[name_col] = anon.text(row[name_col])
            if quantity_col is not None:
                new[quantity_col] = _scale(row[quantity_col], "0.001")
            for i in value_cols:
                if i < len(new):
                    new[i] = _scale(row[i], "0.01")
            out_rows.append(new)
            stats["data_rows"] += 1
        else:
            # Disclaimers and the date footer: keep the shape, replace the prose
            # but preserve the date, which the extractor must still find.
            out_rows.append([anon.text(cell) if not _has_date(cell) else cell for cell in row])
            if any(c.strip() for c in row):
                stats["trailing_rows"] += 1

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(out_rows)
    stats["tokens_replaced"] = anon.replaced_tokens
    stats["accounts"] = len({r[account_col] for r in out_rows[1:] if len(r) >= width and r[0]})
    stats["_account_col"] = account_col
    stats["_name_col"] = name_col if name_col is not None else -1
    return buffer.getvalue(), stats


_DATE_HINT = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|[A-Z][a-z]{2}-\d{1,2}-\d{4}")


def _has_date(cell: str) -> bool:
    return bool(_DATE_HINT.search(cell))


def build_direct_source_pair(anonymized_csv: str) -> dict[str, object]:
    """Build a direct-source document covering some of the anonymized accounts.

    Models the real pair's defining features: the direct source reports fewer
    holdings than the aggregation (a rebalance the feed has not picked up), and
    reports settlement cash outside its holdings table.
    """
    rows = [
        r
        for r in csv.reader(io.StringIO(anonymized_csv))
        if len(r) >= 16 and r[0].strip() and r[0] != "Account number"
    ]
    by_account: dict[str, list[list[str]]] = {}
    for row in rows:
        by_account.setdefault(row[0], []).append(row)

    # Pick the two largest aggregated accounts that carry an embedded number.
    def account_value(rows_for: list[list[str]]) -> Decimal:
        total = Decimal(0)
        for row in rows_for:
            try:
                total += Decimal(row[7].replace("$", "").replace(",", "") or 0)
            except InvalidOperation:
                continue
        return total

    candidates = [
        (key, rows_for)
        for key, rows_for in by_account.items()
        if len(key) == 36 and _LONG_DIGITS.search(rows_for[0][1])
    ]
    candidates.sort(key=lambda kv: account_value(kv[1]), reverse=True)

    accounts: list[dict[str, object]] = []
    for _key, rows_for in candidates[:2]:
        name = rows_for[0][1]
        number = _LONG_DIGITS.findall(name)[-1]
        priced = [r for r in rows_for if r[7].strip() not in {"--", ""}]
        # The direct source knows about fewer holdings, as the real one did.
        kept = priced[: max(1, len(priced) - 2)]
        holdings = sum(Decimal(r[7].replace("$", "").replace(",", "")) for r in kept)
        cash = (holdings * Decimal("0.08")).quantize(Decimal("0.01"))
        accounts.append(
            {
                "external_account_key": f"{number}*",
                "display_name": name.split(" - ")[0],
                "institution": "DirectVest",
                "is_aggregated": False,
                "account_type_hint": None,
                "reported_value": f"${holdings + cash:,.2f}",
                "cash_balance": f"${cash:,.2f}",
                "positions": [
                    {
                        "symbol": r[2] or None,
                        "description": r[3],
                        "quantity": r[4],
                        "price": r[5],
                        "market_value": r[7],
                        "cost_basis": r[13] if r[13].strip() not in {"--", ""} else None,
                        "unrealized_gain_loss": (
                            r[10] if r[10].strip() not in {"--", ""} else None
                        ),
                    }
                    for r in kept
                ],
            }
        )

    return {
        "schema_version": "1.1",
        "document_type": "holdings_web_page",
        "producing_institution": "DirectVest",
        "as_of_date": "08/25/2026",
        "as_of_date_source": "Value as of: August 25, 2026, 4:15 p.m., Eastern time",
        "reported_total_value": None,
        "accounts": accounts,
        "warnings": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(
            f"source not found: {args.source}\n"
            "This script derives fixtures from a private corpus that is not in the "
            "repository. The committed fixtures it produced are already in "
            "tests/fixtures/, so this is only needed to regenerate them.",
            file=sys.stderr,
        )
        return 1

    raw = args.source.read_bytes()
    text = raw.decode("utf-8-sig")
    anonymized, stats = anonymize_csv(text)
    account_col, name_col = stats.pop("_account_col"), stats.pop("_name_col")

    # Preserve the BOM and CRLF: the tests exist to prove we handle them.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes("﻿".encode() + anonymized.encode("utf-8"))

    _verify(text, args.output.read_bytes().decode("utf-8-sig"), account_col, name_col)

    print(f"wrote {args.output.relative_to(REPO_ROOT)}")
    for key, value in stats.items():
        print(f"  {key}: {value}")

    pair = build_direct_source_pair(anonymized)
    PAIR_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    PAIR_OUTPUT.write_text(json.dumps(pair, indent=1) + "\n")
    print(f"wrote {PAIR_OUTPUT.relative_to(REPO_ROOT)}")
    print(f"  accounts: {len(pair['accounts'])}")
    return 0


def _verify(original: str, produced: str, account_col: int, name_col: int) -> None:
    """Fail loudly if any identifying token survived into the output.

    Scoped to the columns that can carry identity -- the account number, the
    account name, and the trailing prose. Ticker symbols and fund descriptions
    are deliberately preserved: they are public instrument names rather than
    personal data, and the fixtures are worthless without them (the split-price
    divergence and the symbol-less 529 holdings are both defined by them).

    This check is the point of the whole script. A silent leak would be
    committed and then very hard to retract.
    """

    def identity_text(text: str) -> str:
        parts: list[str] = []
        for row in csv.reader(io.StringIO(text)):
            if not row:
                continue
            if len(row) > max(account_col, name_col) and row[0].strip():
                parts.append(row[account_col])
                if name_col >= 0:
                    parts.append(row[name_col])
            else:
                parts.extend(row)
        return "\n".join(parts)

    def identifiers(text: str) -> set[str]:
        found = set(_UUID.findall(text))
        found |= set(_LONG_DIGITS.findall(text))
        found |= set(_ACCOUNT_CODE.findall(text))
        # Words only: purely alphabetic, at least three characters, and not a
        # run of hex digits. Hex is excluded because a UUID's own characters
        # would otherwise register as words (a substring like "c-c" spanning a
        # hyphen), and UUIDs are already checked exactly by _UUID above.
        found |= {
            t
            for t in re.findall(r"[A-Za-z]{3,}", text)
            if t.upper() not in PRESERVE and not _HEX_ONLY.fullmatch(t)
        }
        return found

    leaked = identifiers(identity_text(original)) & identifiers(identity_text(produced))
    if leaked:
        raise SystemExit(
            f"REFUSING TO WRITE: {len(leaked)} identifying token(s) survived "
            f"anonymisation, e.g. {sorted(leaked)[:5]}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
