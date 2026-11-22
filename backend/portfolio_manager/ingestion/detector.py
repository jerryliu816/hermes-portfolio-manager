"""Document type detection and deterministic pre-cleaning.

Everything here happens *before* a model sees the document, and none of it is
guesswork -- these are the concrete quirks of the real exports:

    UTF-8 BOM              stripped on decode
    CRLF line endings      normalised
    trailing comma         17 fields against a 16-field header
    quoted commas          inside account names
    blank line + prose     three disclaimer rows before the data ends
    footer-only date       the only date in the file, below the disclaimers

Doing this deterministically keeps it testable and keeps the model's attention
on the part that actually needs judgement.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from ..extraction.contracts import DocumentKind

#: A PDF always starts with this.
_PDF_MAGIC = b"%PDF"

#: Header cells that identify a positions export.
_POSITION_HEADER_HINTS = (
    "account number",
    "account name",
    "symbol",
    "quantity",
    "current value",
    "market value",
)


def detect_kind(filename: str, content: bytes) -> DocumentKind:
    """Detect by content first, extension second.

    Content wins because a mislabelled file is more likely than a mislabelled
    magic number, and feeding a PDF to the CSV path produces nonsense.
    """
    if content.startswith(_PDF_MAGIC):
        return DocumentKind.PDF
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        return DocumentKind.PDF
    if lowered.endswith(".csv"):
        return DocumentKind.CSV
    sample = content[:4096].decode("utf-8-sig", errors="replace").lower()
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    if sum(hint in first_line for hint in _POSITION_HEADER_HINTS) >= 2:
        return DocumentKind.CSV
    return DocumentKind.TEXT


@dataclass
class CleanedCsv:
    """A CSV split into the parts that mean different things."""

    header: list[str]
    rows: list[list[str]]
    #: Free text after the data: disclaimers, and often the only date.
    trailing_lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        return len(self.header)


def clean_csv(text: str) -> CleanedCsv:
    """Split a positions CSV into header, data rows and trailing prose.

    A data row is one with at least the header's column count and a non-empty
    first cell. Rows with fewer cells are prose: disclaimers, the download-date
    footer, and blank separators.
    """
    parsed = list(csv.reader(io.StringIO(text)))
    if not parsed:
        return CleanedCsv(header=[], rows=[], notes=["document is empty"])

    header = [cell.strip().lstrip("﻿") for cell in parsed[0]]
    width = len(header)
    rows: list[list[str]] = []
    trailing: list[str] = []
    notes: list[str] = []
    ragged = 0

    for raw in parsed[1:]:
        if len(raw) >= width and raw and raw[0].strip():
            if len(raw) > width:
                ragged += 1
                # A trailing comma yields one extra empty field; drop only
                # genuinely empty overflow so real data is never truncated.
                overflow = raw[width:]
                if all(not cell.strip() for cell in overflow):
                    raw = raw[:width]
                else:
                    notes.append(
                        f"row for account {raw[0]!r} has {len(raw)} fields against a "
                        f"{width}-field header, with non-empty overflow"
                    )
            rows.append(raw)
        elif any(cell.strip() for cell in raw):
            trailing.append(" ".join(cell for cell in raw if cell.strip()))

    if ragged:
        notes.append(f"{ragged} row(s) carried a trailing empty field, which was dropped")
    return CleanedCsv(header=header, rows=rows, trailing_lines=trailing, notes=notes)
