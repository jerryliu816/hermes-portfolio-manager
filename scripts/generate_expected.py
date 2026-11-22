#!/usr/bin/env python3
"""Produce the expected readings the benchmark scores against.

The baseline must not itself be a model's opinion, so it comes from the mock
provider's deterministic CSV parse -- a plain reader with no judgement in it.
That makes the benchmark a test of whether another provider agrees with an
unambiguous reading of the file, which is the only thing worth measuring.

Regenerating is a deliberate act: if a provider disagrees with the baseline,
the question is which one is right, and answering it by overwriting the
baseline would quietly define the benchmark as "whatever we do now".

Usage:
    python scripts/generate_expected.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from portfolio_manager.extraction.contracts import (  # noqa: E402
    ExtractionContext,
    SourceDocument,
)
from portfolio_manager.extraction.providers.mock_provider import (  # noqa: E402
    MockExtractionProvider,
)

CORPUS = REPO_ROOT / "tests" / "fixtures" / "benchmark"


def read(path: Path) -> str:
    provider = MockExtractionProvider()
    document = SourceDocument.from_path(path)
    overview = provider.understand_document(document, ExtractionContext()).overview
    result = provider.extract_positions(document, overview, ExtractionContext())
    return json.dumps(result.document.model_dump(), indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if any expected reading is out of date, without rewriting it",
    )
    args = parser.parse_args(argv)

    documents = sorted(p for p in CORPUS.glob("*.csv"))
    if not documents:
        print(f"no documents found in {CORPUS}", file=sys.stderr)
        return 1

    stale: list[str] = []
    for document in documents:
        expected_path = document.with_suffix("").with_suffix("")
        expected_path = document.parent / f"{document.stem}.expected.json"
        produced = read(document)
        if args.check:
            if not expected_path.exists() or expected_path.read_text() != produced:
                stale.append(document.name)
        else:
            expected_path.write_text(produced)
            print(f"  wrote {expected_path.relative_to(REPO_ROOT)}")

    if args.check:
        if stale:
            print(
                "expected readings are out of date for: " + ", ".join(stale),
                file=sys.stderr,
            )
            print(
                "Run scripts/generate_expected.py, then review the diff — a changed "
                "baseline is a claim that the previous reading was wrong.",
                file=sys.stderr,
            )
            return 1
        print(f"all {len(documents)} expected readings are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
