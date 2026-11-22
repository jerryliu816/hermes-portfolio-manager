"""What the user wants the advisor to take into account.

Views ("I expect rates to fall within a year"), standing preferences ("no
individual bank stocks"), pages to read, and documents. All of it is opinion or
background -- never a record of what is held -- so it lives in its own table and
its own directory, apart from the import pipeline's sources.

Two rules shape this module:

- **Text is never guessed.** A PDF without a text layer is stored as
  `no_text`, and the advisor is told so; a URL whose content has not been
  captured is `not_captured`, and the advisor reads it live.
- **An advisor's paraphrase is not the user's word.** Items the advisor records
  from a chat are `confirmed=False` until the user says otherwise.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import REPO_ROOT, Settings, get_settings
from ..models.advisory import AdvisorContext
from ..models.enums import ContextCreator, ContextKind, TextExtraction

#: A page yielding fewer characters than this is treated as having no text
#: layer. A scanned page often still yields a stray page number.
EMPTY_PAGE_CHARS = 20

#: Plain-text documents accepted as-is.
_TEXT_SUFFIXES = {".txt", ".md", ".markdown"}


class ContextError(ValueError):
    """A context item could not be stored as given."""


@dataclass(frozen=True)
class ContextListing:
    #: In force: active, and not past its expiry date.
    current: list[AdvisorContext]
    #: Active but past `expires_on`. Listed separately so the advisor can ask
    #: whether a view still holds, rather than silently acting on or dropping it.
    expired: list[AdvisorContext]


@dataclass(frozen=True)
class ExtractedText:
    text: str
    extraction: TextExtraction
    #: 1-based pages that yielded no text, when some did and some did not.
    empty_pages: list[int]


def _clean(value: str, field: str) -> str:
    text = value.strip()
    if not text:
        raise ContextError(f"{field} is empty")
    return text


def add_note(
    session: Session,
    *,
    kind: ContextKind,
    title: str,
    body: str,
    expires_on: dt.date | None = None,
    created_by: ContextCreator = ContextCreator.USER,
) -> AdvisorContext:
    """Record a view or a preference, in the words given."""
    if kind not in (ContextKind.VIEW, ContextKind.PREFERENCE):
        raise ContextError(f"a note is a view or a preference, not {kind.value}")
    record = AdvisorContext(
        kind=kind.value,
        title=_clean(title, "title"),
        body=_clean(body, "body"),
        extraction=TextExtraction.NOT_APPLICABLE.value,
        expires_on=expires_on,
        is_active=True,
        created_by=created_by.value,
        confirmed=created_by is ContextCreator.USER,
    )
    session.add(record)
    session.flush()
    return record


def _validate_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ContextError(f"not an http(s) URL: {url!r}")
    return cleaned


def add_url(
    session: Session,
    *,
    url: str,
    title: str | None = None,
    note: str = "",
    captured_text: str | None = None,
    expires_on: dt.date | None = None,
    created_by: ContextCreator = ContextCreator.USER,
) -> AdvisorContext:
    """Record a page for the advisor to read.

    Without `captured_text` the advisor fetches the page live, which is usually
    what is wanted for news. Capturing text pins the content, for a page that
    may change or disappear.
    """
    cleaned = _validate_url(url)
    body_parts = [note.strip()] if note.strip() else []
    if captured_text is not None and captured_text.strip():
        body_parts.append(captured_text.strip())
        extraction = TextExtraction.EXTRACTED
    else:
        extraction = TextExtraction.NOT_CAPTURED
    record = AdvisorContext(
        kind=ContextKind.URL.value,
        title=(title or "").strip() or cleaned,
        body="\n\n".join(body_parts),
        extraction=extraction.value,
        source_url=cleaned,
        expires_on=expires_on,
        is_active=True,
        created_by=created_by.value,
        confirmed=created_by is ContextCreator.USER,
    )
    session.add(record)
    session.flush()
    return record


def extract_pdf_text(content: bytes) -> ExtractedText:
    """The text layer of a PDF, page by page, with gaps stated.

    Raises for a file that is not a readable PDF: an unparseable document is an
    error, not an empty one.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ContextError("the PDF is encrypted; remove the password and upload it again")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except PdfReadError as exc:
        raise ContextError(f"not a readable PDF: {exc}") from exc

    if not pages:
        raise ContextError("the PDF has no pages")
    empty = [i + 1 for i, text in enumerate(pages) if len(text) < EMPTY_PAGE_CHARS]
    if len(empty) == len(pages):
        return ExtractedText(text="", extraction=TextExtraction.NO_TEXT, empty_pages=empty)
    text = "\n\n".join(
        f"[page {i + 1}]\n{text}" for i, text in enumerate(pages) if i + 1 not in empty
    )
    return ExtractedText(text=text, extraction=TextExtraction.EXTRACTED, empty_pages=empty)


def _extract(filename: str, content: bytes) -> ExtractedText:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" or content.startswith(b"%PDF"):
        return extract_pdf_text(content)
    if suffix in _TEXT_SUFFIXES:
        try:
            text = content.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ContextError(f"{filename} is not UTF-8 text") from exc
        if not text:
            raise ContextError(f"{filename} is empty")
        return ExtractedText(text=text, extraction=TextExtraction.EXTRACTED, empty_pages=[])
    raise ContextError(f"{filename}: only PDF, .txt and .md documents are accepted")


def _store_bytes(settings: Settings, sha256: str, content: bytes) -> str:
    path = settings.context_dir / sha256[:2] / sha256
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)
        # May be a personal document; keep it owner-only like statements.
        path.chmod(0o600)
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def add_document(
    session: Session,
    *,
    filename: str,
    content: bytes,
    title: str | None = None,
    note: str = "",
    expires_on: dt.date | None = None,
    settings: Settings | None = None,
) -> AdvisorContext:
    """Store a document and whatever text it yields.

    The original bytes are preserved, so a better extractor later can re-read
    it. A PDF with no text layer is accepted and marked `no_text`: the user
    learns the advisor cannot read it, rather than the advisor pretending to.
    """
    settings = settings or get_settings()
    if not content:
        raise ContextError(f"{filename} was empty")
    if len(content) > settings.extraction_max_bytes:
        raise ContextError(
            f"{filename} is {len(content)} bytes, over the "
            f"{settings.extraction_max_bytes}-byte limit"
        )
    extracted = _extract(filename, content)
    sha256 = hashlib.sha256(content).hexdigest()
    stored_path = _store_bytes(settings, sha256, content)

    parts = [note.strip()] if note.strip() else []
    if extracted.empty_pages and extracted.extraction is TextExtraction.EXTRACTED:
        listed = ", ".join(str(p) for p in extracted.empty_pages)
        parts.append(f"[no text layer on page(s) {listed}; their content is not included]")
    if extracted.text:
        parts.append(extracted.text)

    record = AdvisorContext(
        kind=ContextKind.DOCUMENT.value,
        title=(title or "").strip() or filename,
        body="\n\n".join(parts),
        extraction=extracted.extraction.value,
        original_filename=filename,
        sha256=sha256,
        stored_path=stored_path,
        expires_on=expires_on,
        is_active=True,
        created_by=ContextCreator.USER.value,
        confirmed=True,
    )
    session.add(record)
    session.flush()
    return record


def get_context(session: Session, context_id: str) -> AdvisorContext | None:
    try:
        key = uuid.UUID(context_id)
    except ValueError:
        return None
    return session.get(AdvisorContext, key)


def list_context(
    session: Session, *, today: dt.date | None = None, include_archived: bool = False
) -> ContextListing:
    """Current and expired items, newest first. Archived items only on request."""
    today = today or dt.date.today()
    statement = select(AdvisorContext).order_by(AdvisorContext.created_at.desc())
    if not include_archived:
        statement = statement.where(AdvisorContext.is_active.is_(True))
    rows = list(session.execute(statement).scalars())
    expired = [r for r in rows if r.is_active and r.expires_on is not None and r.expires_on < today]
    expired_ids = {r.id for r in expired}
    return ContextListing(current=[r for r in rows if r.id not in expired_ids], expired=expired)


def update_context(
    session: Session,
    record: AdvisorContext,
    *,
    title: str | None = None,
    body: str | None = None,
    expires_on: dt.date | None = None,
    clear_expiry: bool = False,
    is_active: bool | None = None,
    confirmed: bool | None = None,
) -> AdvisorContext:
    """Edit an item. Editing an advisor's paraphrase makes it the user's words."""
    if title is not None:
        record.title = _clean(title, "title")
    if body is not None:
        if record.kind in (ContextKind.VIEW.value, ContextKind.PREFERENCE.value):
            record.body = _clean(body, "body")
        else:
            record.body = body
        record.confirmed = True
    if clear_expiry:
        record.expires_on = None
    elif expires_on is not None:
        record.expires_on = expires_on
    if is_active is not None:
        record.is_active = is_active
    if confirmed is not None:
        record.confirmed = confirmed
    session.flush()
    return record


def delete_context(
    session: Session, record: AdvisorContext, settings: Settings | None = None
) -> None:
    """Remove an item, and its preserved file when nothing else refers to it.

    Unlike the journal, context is not a history worth keeping: a document the
    user withdraws should be gone, not merely hidden.
    """
    settings = settings or get_settings()
    sha256, stored = record.sha256, record.stored_path
    session.delete(record)
    session.flush()
    if sha256 and stored:
        still_used = session.execute(
            select(AdvisorContext.id).where(AdvisorContext.sha256 == sha256).limit(1)
        ).first()
        if still_used is None:
            path = Path(stored)
            if not path.is_absolute():
                path = REPO_ROOT / path
            path.unlink(missing_ok=True)
