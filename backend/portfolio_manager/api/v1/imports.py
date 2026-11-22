"""Import endpoints.

Uploading is the one place a user hands the system something it must interpret,
so these responses are deliberately verbose: what was skipped, what could not
be matched, which checks failed and by how much. "AI parsing failed" is not an
acceptable answer when the alternative is a concrete arithmetic difference.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from ...config import Settings
from ...extraction.contracts import ExtractionError
from ...extraction.registry import get_provider
from ...models.enums import SourceRole
from ...schemas.responses import ImportBatchOut, ImportOut
from ...services.importer import (
    DateOverrideError,
    ImportResult,
    import_document,
    import_documents,
)
from ..deps import SessionDep, SettingsDep

router = APIRouter(prefix="/imports", tags=["imports"])


async def _read_upload(upload: UploadFile, settings: Settings) -> bytes:
    contents = await upload.read()
    name = upload.filename or "upload"
    if not contents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} was empty")
    if len(contents) > settings.extraction_max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"{name} is {len(contents)} bytes, over the {settings.extraction_max_bytes}-byte "
            "limit; it will never be silently truncated",
        )
    return contents


@contextmanager
def _translated_errors() -> Iterator[None]:
    try:
        yield
    except DateOverrideError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "kind": "date_override_refused",
                "message": str(exc),
                "stated": exc.stated.isoformat(),
                "requested": exc.requested.isoformat(),
                "tolerance_days": exc.tolerance_days,
            },
        ) from exc
    except ExtractionError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"kind": exc.kind.value, "message": str(exc), "retryable": exc.retryable},
        ) from exc


def _import_out(result: ImportResult) -> ImportOut:
    snapshot = result.snapshot
    multi = result.multi_source
    return ImportOut(
        outcome=result.outcome.value,
        filename=result.source_file.original_filename,
        source_file_id=str(result.source_file.id),
        sha256_prefix=result.source_file.sha256[:12],
        snapshot_id=str(snapshot.snapshot.id) if snapshot else None,
        message=result.message,
        reconciliation=(
            {
                "summary": snapshot.reconciliation.summary(),
                "failures": list(snapshot.reconciliation.failure_descriptions()),
                "cash_residuals": {
                    k: str(v) for k, v in snapshot.reconciliation.cash_residuals.items()
                },
            }
            if snapshot
            else None
        ),
        validation=snapshot.validation.summary() if snapshot else None,
        joined=multi is not None,
        unmatched_accounts=list(multi.unmatched_accounts) if multi else [],
    )


@router.post("", response_model=ImportOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="A brokerage export: CSV or PDF")],
    provider: str | None = None,
    as_of: dt.date | None = None,
    force_as_of: bool = False,
) -> ImportOut:
    """Import a document, or report that it was already imported.

    Re-uploading an identical file is a no-op that returns the prior import:
    deduplication is by content hash, so an accidental double upload cannot
    create a second snapshot.

    A document whose date already has a snapshot joins it and supplies the
    accounts it wins precedence for. `as_of` places the document on a chosen
    date instead of the one it states, which is how two documents exported a few
    days apart are filed as one portfolio.
    """
    contents = await _read_upload(file, settings)

    suffix = Path(file.filename or "upload").suffix or ".csv"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(contents)
        temporary = Path(handle.name)

    try:
        with _translated_errors():
            result = import_document(
                session,
                temporary,
                get_provider(provider, settings),
                role=SourceRole.AUTHORITATIVE,
                as_of_date=as_of,
                force_as_of=force_as_of,
                settings=settings,
            )
    finally:
        temporary.unlink(missing_ok=True)

    # Keep the user's own filename rather than the temp file's.
    result.source_file.original_filename = file.filename or result.source_file.original_filename
    session.flush()
    return _import_out(result)


@router.post("/batch", response_model=ImportBatchOut, status_code=status.HTTP_201_CREATED)
async def upload_documents(
    session: SessionDep,
    settings: SettingsDep,
    files: Annotated[
        list[UploadFile], File(description="Brokerage exports describing one portfolio")
    ],
    provider: str | None = None,
    as_of: dt.date | None = None,
    force_as_of: bool = False,
) -> ImportBatchOut:
    """Import several documents as one portfolio.

    For two exports of the same portfolio taken a day or two apart -- an
    aggregation that sees every account, and a direct source that is fresher
    for the accounts it covers. They are filed as one snapshot without any date
    being typed: the widest document goes first and sets the date, and the
    rest join it, winning the accounts precedence gives them. Sent separately,
    the narrower one would open a snapshot of its own and read as a partial
    portfolio.

    One file behaves exactly like `POST /imports`.
    """
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no files were uploaded")
    names = [Path(f.filename or f"upload-{i}").name for i, f in enumerate(files)]
    if len(set(names)) != len(names):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "two uploaded files have the same name; rename one"
        )
    contents = [await _read_upload(f, settings) for f in files]

    # The files keep the user's own names on disk, because filing order and
    # the stored record both report documents by name.
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for name, data in zip(names, contents, strict=True):
            path = Path(directory) / name
            path.write_bytes(data)
            paths.append(path)
        with _translated_errors():
            outcome = import_documents(
                session,
                paths,
                get_provider(provider, settings),
                role=SourceRole.AUTHORITATIVE,
                as_of_date=as_of,
                force_as_of=force_as_of,
                settings=settings,
            )

    session.flush()
    return ImportBatchOut(
        results=[_import_out(r) for r in outcome.results],
        order=list(outcome.order),
        reordered=outcome.reordered,
        as_of_date=outcome.as_of_date,
    )
