"""Nothing derived from the private corpus may live in the repository.

`test/`, `data/` and `.env` are gitignored, and a pre-commit hook blocks them.
That is not enough on its own: the leak this catches is the quiet one, where a
real figure is copied into a test because it was in front of you and made the
case feel concrete.

Beyond privacy, there is a correctness reason. A balance or a position count
keyed to the real portfolio goes stale the moment a new statement is imported.
Anything a test depends on should be fixed by the fixture, not by what the
owner happened to hold on the day it was written.

The identifiers are read from the private corpus at run time rather than listed
here -- writing them down in order to detect them would be the very thing being
prevented. For the same reason a failure names the file and line, never the
value: the value is in the owner's statements already, and a test log is one
more place it could be copied from.

Two sources make up the corpus:

* `test/` -- the hand-kept export folder, when present.
* `data/sources/` -- every original document the app has imported. This one is
  present wherever the app has been used, so the check no longer depends on a
  folder that is usually absent.

It was once read only from `test/`, and it skipped -- silently, on the owner's
machine, for weeks -- while eight real figures sat in tests, prompts and docs.
A skip now means there are no statements on this machine at all. Statements
that exist but yield nothing to check against are a failure.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIRS = (REPO_ROOT / "test", REPO_ROOT / "data" / "sources")

#: Text a tracked file can hide a figure in. Fixtures are included: they are
#: anonymized derivatives, and a real figure surviving anonymization is exactly
#: the kind of leak this exists to catch.
SCANNED_SUFFIXES = (
    ".py", ".md", ".ts", ".tsx", ".json", ".yml", ".yaml", ".toml", ".sh",
    ".csv", ".txt", ".html", ".css", ".ini", ".cfg", ".plist", ".template",
)  # fmt: skip

#: Words that appear in real account names but carry no identity: the account
#: type vocabulary the classifier is built around.
STRUCTURAL = {
    "brokerage",
    "account",
    "rollover",
    "ira",
    "roth",
    "traditional",
    "joint",
    "wros",
    "tod",
    "corporation",
    "individual",
    "college",
    "savings",
    "plan",
    "equity",
    "trust",
    "hsa",
    "inc",
    "the",
    "and",
    "llc",
    "fund",
    "index",
}

#: Export columns whose values are the owner's alone. Prices, price changes and
#: percentages are left out on purpose: prices are public market data, and the
#: anonymizer keeps them so `quantity x price = value` still holds.
PRIVATE_COLUMNS = ("Quantity", "Current value", "Total gain/loss dollar", "Cost basis total")
PRICE_COLUMN = "Last price"

#: Below this many significant digits a number collides with ordinary test
#: values too often to mean anything ("1.50", "100.25").
MIN_SIGNIFICANT_DIGITS = 5


# --- reading the corpus ------------------------------------------------------


@dataclass
class Corpus:
    files: list[Path] = field(default_factory=list)
    names: set[str] = field(default_factory=set)
    identifiers: set[str] = field(default_factory=set)
    figures: set[Decimal] = field(default_factory=set)
    prices: set[Decimal] = field(default_factory=set)
    #: Real account-key endings, for spotting a masked `****1234` copied out.
    key_suffixes: set[str] = field(default_factory=set)


def _private_csv() -> Path | None:
    """The newest hand-kept Fidelity export in `test/`, if there is one.

    Used by `test_anonymized_corpus.py` to compare the fixture against the file
    it was derived from. Dated exports are matched by shape, not one name: a
    pinned filename once stopped resolving and that check skipped silently.
    """
    candidates = sorted((REPO_ROOT / "test").glob("Fidelity*.csv"))
    return candidates[-1] if candidates else None


def _decimal(cell: str) -> Decimal | None:
    cleaned = cell.strip().replace("$", "").replace(",", "").replace("+", "").strip("()")
    cleaned = cleaned.lstrip("-")
    if not re.fullmatch(r"\d+\.\d+", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _distinctive(value: Decimal) -> bool:
    """Specific enough that finding it in the repository is not coincidence."""
    digits = format(value.normalize(), "f").replace(".", "").lstrip("0")
    return len(digits) >= MIN_SIGNIFICANT_DIGITS and value != value.to_integral_value()


def _read_csv(text: str, corpus: Corpus) -> None:
    header: list[str] | None = None
    for row in csv.reader(io.StringIO(text)):
        if row and row[0].strip() == "Account number":
            header = [h.strip() for h in row]
            continue
        if header is None or len(row) < len(header) or not row[0].strip():
            continue
        cell = dict(zip(header, row, strict=False))
        key, name = cell.get("Account number", ""), cell.get("Account name", "")

        # Deliberately no leading \b: real account keys can carry a letter
        # prefix (shaped like "X12345678"), and there is no word boundary
        # between that letter and the digits.
        corpus.identifiers.update(re.findall(r"\d{6,}", f"{key} {name}"))
        corpus.identifiers.update(re.findall(r"[A-Za-z0-9-]{8,}", key))
        stripped = key.strip()
        if len(stripped) >= 4:
            corpus.key_suffixes.add(stripped[-4:].lower())
        for token in re.findall(r"[A-Za-z]{3,}", name):
            if token.lower() not in STRUCTURAL:
                corpus.names.add(token.lower())

        for column in PRIVATE_COLUMNS:
            value = _decimal(cell.get(column, ""))
            if value is not None and _distinctive(value):
                corpus.figures.add(value)
        price = _decimal(cell.get(PRICE_COLUMN, ""))
        if price is not None:
            corpus.prices.add(price)


def _read_pdf(path: Path, corpus: Corpus) -> None:
    """Statement PDFs: every specific amount and share count in them.

    A PDF has no columns to tell a price from a balance, so this keeps the two
    shapes that are never prices -- amounts of four figures or more, and
    three-decimal share counts -- and then drops anything an export lists as a
    price.
    """
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    for match in re.findall(r"\$?\(?-?[0-9][0-9,]*\.[0-9]{2,4}\)?", text):
        value = _decimal(match)
        if value is None or not _distinctive(value):
            continue
        places = -value.as_tuple().exponent  # type: ignore[operator]
        if places >= 3 or value >= 1000:
            corpus.figures.add(value)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    found = Corpus()
    for directory in CORPUS_DIRS:
        if directory.is_dir():
            found.files.extend(p for p in sorted(directory.rglob("*")) if p.is_file())
    found.files = [p for p in found.files if not p.name.startswith(".")]
    if not found.files:
        pytest.skip("no private statements on this machine; nothing to check against")

    for path in found.files:
        raw = path.read_bytes()
        if raw[:4] == b"%PDF":
            _read_pdf(path, found)
        else:
            _read_csv(raw.decode("utf-8-sig", errors="ignore"), found)
    found.figures -= found.prices

    # Statements present but nothing read out of them means the reader no
    # longer understands the format. Passing would be the silent failure this
    # module was rewritten to stop.
    assert found.figures, f"read {len(found.files)} statement file(s) but found no figures"
    assert found.identifiers, f"read {len(found.files)} statement file(s) but no account keys"
    return found


# --- searching the repository ------------------------------------------------


def _tracked_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [
        REPO_ROOT / line
        for line in listing.stdout.splitlines()
        if line.endswith(SCANNED_SUFFIXES) and (REPO_ROOT / line).is_file()
    ]


def _figure_forms(value: Decimal) -> set[str]:
    """`48207.66` and `48,207.66`: both export styles print real money."""
    plain = format(value, "f")
    whole, _, frac = plain.partition(".")
    return {plain, f"{int(whole):,}.{frac}"}


#: A copyright notice exists to name its author, and the owner publishes under
#: their own name. Only the name check honours this, and only on a line that
#: *starts* with the notice -- a name anywhere else is still a leak.
COPYRIGHT_LINE = re.compile(r"^\W*Copyright\b")


def _locate(
    pattern: re.Pattern[str], files: list[Path], *, allow_copyright: bool = False
) -> list[str]:
    """`path:line` for every match. Never the matched text."""
    hits: list[str] = []
    for path in files:
        text = path.read_text(errors="ignore")
        lines = text.splitlines()
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            if allow_copyright and COPYRIGHT_LINE.match(lines[line - 1]):
                continue
            hits.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    return hits


def _alternation(needles: set[str]) -> str:
    return "|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True))


def _report(kind: str, hits: list[str], advice: str) -> str:
    shown = "\n  ".join(sorted(set(hits))[:20])
    return f"{len(set(hits))} real {kind} tracked in the repository:\n  {shown}\n{advice}"


# --- the checks --------------------------------------------------------------


def test_no_real_personal_name_is_tracked(corpus: Corpus) -> None:
    """A test proving a name is unclassifiable needs *a* name, not *this* one.

    Case-insensitive with a word boundary: an earlier version matched only
    capitalised words and missed a real first name used as an upper-case
    constant. Three-letter tokens collide with ordinary English too often to
    fold case, so they stay capitalised-only.
    """
    long_names = {n for n in corpus.names if len(n) >= 4}
    short_names = {n.capitalize() for n in corpus.names if len(n) == 3}
    files = _tracked_files()
    hits: list[str] = []
    if long_names:
        long_pattern = re.compile(rf"\b(?:{_alternation(long_names)})\b", re.I)
        hits += _locate(long_pattern, files, allow_copyright=True)
    if short_names:
        short_pattern = re.compile(rf"\b(?:{_alternation(short_names)})\b")
        hits += _locate(short_pattern, files, allow_copyright=True)
    assert not hits, _report("names", hits, "Use an obviously invented name.")


def test_no_real_account_identifier_is_tracked(corpus: Corpus) -> None:
    """The tested property is that identifiers collide, not which ones do."""
    pattern = re.compile(_alternation(corpus.identifiers))
    hits = _locate(pattern, _tracked_files())
    assert not hits, _report("account identifiers", hits, "Use an obvious fake like X1.")


def test_no_masked_real_account_ending_is_tracked(corpus: Corpus) -> None:
    """`****1a2b` is still the real account's ending."""
    hits: list[str] = []
    masked = re.compile(r"\*{3,}([A-Za-z0-9]{4})\b")
    for path in _tracked_files():
        text = path.read_text(errors="ignore")
        for match in masked.finditer(text):
            if match.group(1).lower() in corpus.key_suffixes:
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    assert not hits, _report("account endings", hits, "Use an invented ending like ****0001.")


def test_no_real_figure_is_tracked(corpus: Corpus) -> None:
    """Values, cost bases, gains, cash, totals and share counts -- all of them.

    Each is matched in both number styles and only as a whole number, so
    `8207.66` does not match inside `48207.66`. A hit can, rarely, be an
    invented number that happens to equal a real one; pick a different one
    rather than weakening this.
    """
    needles: set[str] = set()
    for value in corpus.figures:
        needles |= _figure_forms(value)
    pattern = re.compile(rf"(?<![\d.,])(?:{_alternation(needles)})(?!\d)")
    hits = _locate(pattern, _tracked_files())
    assert not hits, _report(
        "figures",
        hits,
        "Replace each with an invented number that keeps the test's arithmetic "
        "(see 'Every number in a test, example or comment is invented' in CLAUDE.md).",
    )


def test_the_guard_reads_every_statement_it_finds(corpus: Corpus) -> None:
    """The failure mode this guard is most exposed to is its own silence.

    It skipped for a while because the corpus had been renamed, then because it
    looked in a folder that was usually absent. Every statement on disk must be
    a CSV export this module parses or a PDF it can read.
    """
    unread = [
        p.name
        for p in corpus.files
        if not p.read_bytes().startswith((b"%PDF", b"\xef\xbb\xbfAccount number", b"Account"))
    ]
    assert not unread, f"statement files this guard cannot read: {unread}"
