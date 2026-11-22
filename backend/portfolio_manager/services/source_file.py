"""Preservation and deduplication of imported documents.

The original bytes are kept forever under `data/sources/<sha[:2]>/<sha>`. That
matters because extraction is a judgement call made by a model: if the schema
changes, or a provider improves, or a number is ever disputed, the only way to
re-derive the truth is to still have the document that produced it.

Deduplication is by content hash, not filename. Re-importing the identical file
is a no-op that returns the prior record -- so an accidental double upload
cannot create a second snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import REPO_ROOT, Settings, get_settings
from ..extraction.contracts import SourceDocument
from ..models.entities import SourceFile
from ..models.enums import SourceRole


@dataclass(frozen=True)
class StoredSource:
    source_file: SourceFile
    #: False when an identical file had already been imported.
    is_new: bool


def _storage_path(settings: Settings, sha256: str) -> Path:
    """Fan out by the first two hex characters to keep directories small."""
    return settings.sources_dir / sha256[:2] / sha256


def store_source_document(
    session: Session,
    document: SourceDocument,
    *,
    role: SourceRole = SourceRole.AUTHORITATIVE,
    settings: Settings | None = None,
) -> StoredSource:
    """Persist a document's bytes and record, or return the existing record.

    The bytes are written before the row is committed, and writing is
    idempotent: a file already present with the right hash is left alone.
    """
    settings = settings or get_settings()

    existing = session.execute(
        select(SourceFile).where(SourceFile.sha256 == document.sha256)
    ).scalar_one_or_none()
    if existing is not None:
        return StoredSource(source_file=existing, is_new=False)

    path = _storage_path(settings, document.sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(document.content)
        # Statements are personal financial records; keep them owner-only.
        path.chmod(0o600)

    record = SourceFile(
        sha256=document.sha256,
        original_filename=document.filename,
        content_type=_content_type(document),
        byte_size=len(document.content),
        role=role.value,
        stored_path=_relative_to_repo(path),
    )
    session.add(record)
    session.flush()
    return StoredSource(source_file=record, is_new=True)


def _relative_to_repo(path: Path) -> str:
    """Store a repo-relative path so the database survives a move of the checkout."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _content_type(document: SourceDocument) -> str:
    return {
        "pdf": "application/pdf",
        "csv": "text/csv",
        "text": "text/plain",
    }[document.kind.value]


def load_stored_bytes(source_file: SourceFile, settings: Settings | None = None) -> bytes:
    """Read a preserved document back, for re-extraction."""
    settings = settings or get_settings()
    path = Path(source_file.stored_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        # Fall back to the canonical location, e.g. after a checkout move.
        path = _storage_path(settings, source_file.sha256)
    return path.read_bytes()
