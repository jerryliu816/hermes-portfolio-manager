"""What the advisor reads besides the portfolio: philosophy and context.

The philosophy is versioned prose. Context items are views, preferences, URLs
and documents. Both are advisory input -- neither is portfolio truth, and
nothing here can change a holding.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from ...models.advisory import AdvisorContext, InvestmentPhilosophy
from ...models.enums import ContextKind
from ...schemas.responses import AdvisorContextListOut, AdvisorContextOut, PhilosophyOut
from ...services.advisor_context import (
    ContextError,
    add_document,
    add_note,
    add_url,
    delete_context,
    get_context,
    list_context,
    update_context,
)
from ...services.philosophy import (
    STARTER_PHILOSOPHY,
    PhilosophyError,
    active_philosophy,
    philosophy_history,
    save_philosophy,
)
from ..deps import SessionDep, SettingsDep

router = APIRouter(prefix="/advisor", tags=["advisor"])


def _philosophy_out(record: InvestmentPhilosophy) -> PhilosophyOut:
    return PhilosophyOut(
        version=record.version,
        is_active=record.is_active,
        body_markdown=record.body_markdown,
        created_at=record.created_at,
    )


def context_out(record: AdvisorContext, today: dt.date | None = None) -> AdvisorContextOut:
    today = today or dt.date.today()
    return AdvisorContextOut(
        id=str(record.id),
        kind=record.kind,
        title=record.title,
        body=record.body,
        extraction=record.extraction,
        source_url=record.source_url,
        original_filename=record.original_filename,
        expires_on=record.expires_on,
        is_expired=record.expires_on is not None and record.expires_on < today,
        is_active=record.is_active,
        created_by=record.created_by,
        confirmed=record.confirmed,
        created_at=record.created_at,
    )


# --- philosophy -------------------------------------------------------------


class PhilosophyIn(BaseModel):
    body_markdown: str = Field(description="The whole philosophy, in your own words")


@router.get("/philosophy", response_model=PhilosophyOut)
def get_philosophy(session: SessionDep) -> PhilosophyOut:
    record = active_philosophy(session)
    if record is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no investment philosophy has been written; GET /advisor/philosophy/starter",
        )
    return _philosophy_out(record)


@router.get(
    "/philosophy/starter",
    response_class=Response,
    responses={200: {"content": {"text/markdown": {}}}},
)
def get_starter_philosophy() -> Response:
    """Questions to answer, not answers: a starter is never advice."""
    return Response(content=STARTER_PHILOSOPHY, media_type="text/markdown")


@router.get("/philosophy/history", response_model=list[PhilosophyOut])
def get_philosophy_history(session: SessionDep) -> list[PhilosophyOut]:
    return [_philosophy_out(r) for r in philosophy_history(session)]


@router.put("/philosophy", response_model=PhilosophyOut)
def put_philosophy(payload: PhilosophyIn, session: SessionDep) -> PhilosophyOut:
    """Save a new version. The previous one is retired, never overwritten."""
    try:
        return _philosophy_out(save_philosophy(session, payload.body_markdown))
    except PhilosophyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


# --- context ----------------------------------------------------------------


class NoteIn(BaseModel):
    kind: Literal["view", "preference"]
    title: str
    body: str
    expires_on: dt.date | None = Field(
        default=None, description="When this stops being true, if it ever does"
    )


class UrlIn(BaseModel):
    url: str
    title: str | None = None
    note: str = Field(default="", description="Why it matters, in your words")
    expires_on: dt.date | None = None


class ContextPatchIn(BaseModel):
    title: str | None = None
    body: str | None = None
    expires_on: dt.date | None = None
    clear_expiry: bool = False
    is_active: bool | None = None
    confirmed: bool | None = None


def _found(session: SessionDep, context_id: str) -> AdvisorContext:
    record = get_context(session, context_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"context item {context_id} not found")
    return record


@router.get("/context", response_model=AdvisorContextListOut)
def get_context_items(session: SessionDep, include_archived: bool = False) -> AdvisorContextListOut:
    today = dt.date.today()
    listing = list_context(session, today=today, include_archived=include_archived)
    return AdvisorContextListOut(
        current=[context_out(r, today) for r in listing.current],
        expired=[context_out(r, today) for r in listing.expired],
    )


@router.post("/context/notes", response_model=AdvisorContextOut, status_code=201)
def post_note(payload: NoteIn, session: SessionDep) -> AdvisorContextOut:
    try:
        record = add_note(
            session,
            kind=ContextKind(payload.kind),
            title=payload.title,
            body=payload.body,
            expires_on=payload.expires_on,
        )
    except ContextError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return context_out(record)


@router.post("/context/urls", response_model=AdvisorContextOut, status_code=201)
def post_url(payload: UrlIn, session: SessionDep) -> AdvisorContextOut:
    """Record a page. The advisor fetches it live when it needs the content."""
    try:
        record = add_url(
            session,
            url=payload.url,
            title=payload.title,
            note=payload.note,
            expires_on=payload.expires_on,
        )
    except ContextError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return context_out(record)


@router.post("/context/documents", response_model=AdvisorContextOut, status_code=201)
async def post_document(
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="A PDF, .txt or .md document")],
    title: Annotated[str | None, Form()] = None,
    note: Annotated[str, Form()] = "",
    expires_on: Annotated[dt.date | None, Form()] = None,
) -> AdvisorContextOut:
    """Store a document and its text. A PDF with no text layer is kept and
    marked `no_text`, so you know the advisor cannot read it."""
    content = await file.read()
    try:
        record = add_document(
            session,
            filename=file.filename or "document",
            content=content,
            title=title,
            note=note,
            expires_on=expires_on,
            settings=settings,
        )
    except ContextError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return context_out(record)


@router.patch("/context/{context_id}", response_model=AdvisorContextOut)
def patch_context(
    context_id: str, payload: ContextPatchIn, session: SessionDep
) -> AdvisorContextOut:
    record = _found(session, context_id)
    try:
        update_context(
            session,
            record,
            title=payload.title,
            body=payload.body,
            expires_on=payload.expires_on,
            clear_expiry=payload.clear_expiry,
            is_active=payload.is_active,
            confirmed=payload.confirmed,
        )
    except ContextError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return context_out(record)


@router.delete("/context/{context_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_context(context_id: str, session: SessionDep, settings: SettingsDep) -> Response:
    delete_context(session, _found(session, context_id), settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
