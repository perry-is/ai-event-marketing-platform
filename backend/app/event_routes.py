"""Authenticated API for Event intake, source preservation and clarification."""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import select

from app import auth
from app.campaign_service import material_event_snapshot
from app.event_service import (
    accept_proposals,
    answer_clarification,
    can_retry_source,
    create_source_text,
    enforce_source_quota,
    event_by_id,
    event_sources,
    event_view,
    initial_facts,
    initial_intake,
    list_events,
    mark_invalid_answer,
    recompute_clarifications,
    run_source_extraction,
    update_facts,
)
from app.execution_service import invalidate_material_event_approvals
from app.file_storage import (
    read_and_validate_upload,
    resolve_storage_path,
    safe_display_name,
    storage_key,
    write_private,
)
from app.models import Event, SourceMaterial

router = APIRouter(prefix="/api/events", dependencies=[Depends(auth.require_session)])


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateEvent(StrictInput):
    text: StrictStr | None = Field(default=None, max_length=2 * 1024 * 1024)
    input_method: Literal["TYPED", "PASTED"] = "PASTED"


class FactChange(StrictInput):
    field_path: StrictStr
    value: StrictStr | None


class UpdateFacts(StrictInput):
    expected_revision: int = Field(ge=1)
    changes: list[FactChange] = Field(min_length=1, max_length=8)


class RevisionRequest(StrictInput):
    expected_revision: int = Field(ge=1)


class DefaultVenueRequest(StrictInput):
    expected_revision: int = Field(ge=1)


class AnswerClarification(StrictInput):
    expected_revision: int = Field(ge=1)
    answer: StrictStr = Field(min_length=1, max_length=2000)


class DescriptionSuggestionRequest(StrictInput):
    instruction: StrictStr | None = Field(default=None, max_length=500)
    tone: Literal["PROFESSIONAL", "INVITING", "SHORTER", "ENERGETIC", "BEGINNER_CLEAR"] | None = (
        None
    )
    current_description: StrictStr | None = Field(default=None, max_length=5000)


class DescriptionSuggestionResponse(BaseModel):
    description: str


class EventSource(BaseModel):
    id: str
    kind: str
    filename: str | None
    content_type: str
    byte_count: int
    processing_status: str
    can_retry_extraction: bool
    created_at: str


class Proposal(BaseModel):
    id: str
    field_path: str
    candidate_value: object
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    source_id: str
    locator: str
    short_excerpt: str
    uncertainty_reason: str | None
    status: str


class ClarificationCandidate(BaseModel):
    label: str
    value: object
    source_id: str | None
    reason: str | None = None


class Clarification(BaseModel):
    id: str
    kind: str
    field_path: str
    question: str
    candidates: list[ClarificationCandidate]
    status: str
    answer: str | None
    validation_message: str | None = None
    created_at: str
    resolved_at: str | None
    suggested_venue: dict[str, str] | None = None


class EventReadiness(BaseModel):
    ready: bool
    missing_fields: list[str]
    open_question_count: int


class EventView(BaseModel):
    id: str
    revision: int
    title: str | None
    starts_at: str | None
    ends_at: str | None
    timezone: str | None
    delivery: str | None
    facts: dict
    proposals: list[Proposal]
    clarifications: list[Clarification]
    readiness: EventReadiness
    state: str
    created_at: str
    updated_at: str
    sources: list[EventSource]


class EventListItem(BaseModel):
    id: str
    revision: int
    title: str
    starts_at: str | None
    timezone: str | None
    delivery: str | None
    state: str
    readiness: EventReadiness
    updated_at: str


def _view(db, event: Event, settings=None) -> dict:
    view = event_view(event, event_sources(db, event.id))
    location = (event.facts or {}).get("location", {})
    venue = (
        settings.business_default_venue
        if settings and event.delivery in {"IN_PERSON", "HYBRID"} and not location.get("venue")
        else None
    )
    if venue:
        for question in view["clarifications"]:
            if question["status"] == "OPEN" and question["field_path"] == "facts.location.venue":
                question["suggested_venue"] = venue
    return view


@router.get("", response_model=list[EventListItem])
def get_events(request: Request):
    with request.app.state.database.sessions() as db:
        return list_events(db)


@router.post("", response_model=EventView, status_code=201)
def create_event(
    body: CreateEvent,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    text = body.text
    if text is not None and len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(413, "TEXT_TOO_LARGE")
    at = datetime.now(UTC)
    source_id = None
    with request.app.state.database.sessions.begin() as db:
        event = Event(
            id=uuid.uuid4(),
            revision=1,
            title=None,
            starts_at=None,
            ends_at=None,
            timezone=request.app.state.settings.business_default_timezone,
            delivery=None,
            facts=initial_facts(),
            fact_evidence={},
            intake_proposals=initial_intake(),
            internal_notes=None,
            created_by=current.user_id,
            created_at=at,
            updated_at=at,
        )
        db.add(event)
        db.flush()
        if text and text.strip():
            source = create_source_text(db, event, current.user_id, text, body.input_method)
            source_id = source.id
    if source_id:
        run_source_extraction(
            request.app.state.database.sessions,
            request.app.state.settings.private_storage_dir,
            event.id,
            source_id,
            request.app.state.extractor,
            request.app.state.settings.business_default_timezone,
        )
    with request.app.state.database.sessions.begin() as db:
        event = event_by_id(db, event.id)
        if text and not any(
            proposal.get("status") == "PENDING"
            for proposal in event.intake_proposals.get("proposals", [])
        ):
            recompute_clarifications(event)
        return _view(db, event, request.app.state.settings)


@router.post("/{event_id}/sources", response_model=EventView, status_code=201)
def add_source(
    event_id: str,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
    text: Annotated[str | None, Form(max_length=2 * 1024 * 1024)] = None,
    input_method: Annotated[Literal["TYPED", "PASTED"], Form()] = "PASTED",
    file: Annotated[UploadFile | None, File()] = None,
):
    if (text is None or not text.strip()) and file is None:
        raise HTTPException(422, "SOURCE_REQUIRED")
    upload_data = None
    display_name = detected_type = key = None
    if file is not None:
        upload_data, display_name, detected_type = read_and_validate_upload(file)
        key = storage_key()
    if text is not None and len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(413, "TEXT_TOO_LARGE")

    written_path: Path | None = None
    source_ids = []
    try:
        with request.app.state.database.sessions.begin() as db:
            event = event_by_id(db, event_id)
            sources = []
            if text and text.strip():
                text_source = create_source_text(db, event, current.user_id, text, input_method)
                sources.append(text_source)
                source_ids.append(text_source.id)
            if upload_data is not None:
                enforce_source_quota(db, event.id, len(upload_data))
                source = SourceMaterial(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    kind="FILE",
                    original_name=safe_display_name(display_name),
                    storage_key=key,
                    original_text=None,
                    sha256=hashlib.sha256(upload_data).hexdigest(),
                    byte_count=len(upload_data),
                    detected_type=detected_type,
                    input_method="UPLOAD",
                    processing_status="STORED_PENDING_EXTRACTION",
                    extraction={},
                    supplied_by=current.user_id,
                    created_at=datetime.now(UTC),
                )
                db.add(source)
                db.flush()
                written_path = write_private(
                    request.app.state.settings.private_storage_dir, key, upload_data
                )
                sources.append(source)
                source_ids.append(source.id)
            if not any(
                proposal.get("status") == "PENDING"
                for proposal in event.intake_proposals.get("proposals", [])
            ):
                recompute_clarifications(event)
            event.updated_at = datetime.now(UTC)
    except Exception:
        if written_path is not None:
            written_path.unlink(missing_ok=True)
        raise
    for source_id in source_ids:
        run_source_extraction(
            request.app.state.database.sessions,
            request.app.state.settings.private_storage_dir,
            event.id,
            source_id,
            request.app.state.extractor,
            request.app.state.settings.business_default_timezone,
        )
    with request.app.state.database.sessions() as db:
        event = event_by_id(db, event.id)
        return _view(db, event, request.app.state.settings)


@router.get("/{event_id}", response_model=EventView)
def get_event(event_id: str, request: Request):
    with request.app.state.database.sessions() as db:
        event = event_by_id(db, event_id)
        return _view(db, event, request.app.state.settings)


@router.post("/{event_id}/sources/{source_id}/retry-extraction", response_model=EventView)
def retry_source_extraction(
    event_id: str,
    source_id: str,
    request: Request,
):
    from uuid import UUID

    try:
        parsed_event_id, parsed_source_id = UUID(event_id), UUID(source_id)
    except ValueError:
        raise HTTPException(404, "SOURCE_NOT_FOUND") from None
    with request.app.state.database.sessions() as db:
        source = db.scalar(
            select(SourceMaterial).where(
                SourceMaterial.id == parsed_source_id,
                SourceMaterial.event_id == parsed_event_id,
            )
        )
        if source is None:
            raise HTTPException(404, "SOURCE_NOT_FOUND")
        if not can_retry_source(source):
            raise HTTPException(409, "EXTRACTION_RETRY_NOT_AVAILABLE")
    run_source_extraction(
        request.app.state.database.sessions,
        request.app.state.settings.private_storage_dir,
        parsed_event_id,
        parsed_source_id,
        request.app.state.extractor,
        request.app.state.settings.business_default_timezone,
    )
    with request.app.state.database.sessions() as db:
        event = event_by_id(db, parsed_event_id)
        return _view(db, event, request.app.state.settings)


@router.post("/{event_id}/accept-proposals", response_model=EventView)
def accept_event_proposals(
    event_id: str,
    body: RevisionRequest,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    with request.app.state.database.sessions.begin() as db:
        event = event_by_id(db, event_id)
        before = material_event_snapshot(event)
        accept_proposals(db, event, body.expected_revision, current.user_id)
        invalidate_material_event_approvals(db, event, before, current.user_id)
        return _view(db, event, request.app.state.settings)


@router.post("/{event_id}/suggested-venue", response_model=EventView)
def accept_suggested_venue(
    event_id: str,
    body: DefaultVenueRequest,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    venue = request.app.state.settings.business_default_venue
    changes = [
        {"field_path": "facts.location.venue", "value": venue["name"]},
        {
            "field_path": "facts.location.address",
            "value": (
                f"{venue['street']}, {venue['city']}, {venue['region']} {venue['postal_code']}"
            ),
        },
        {"field_path": "facts.location.street", "value": venue["street"]},
        {"field_path": "facts.location.city", "value": venue["city"]},
        {"field_path": "facts.location.region", "value": venue["region"]},
        {"field_path": "facts.location.postal_code", "value": venue["postal_code"]},
        {"field_path": "facts.location.country", "value": venue["country"]},
    ]
    with request.app.state.database.sessions.begin() as db:
        event = event_by_id(db, event_id)
        before = material_event_snapshot(event)
        update_facts(event, body.expected_revision, changes, current.user_id)
        invalidate_material_event_approvals(db, event, before, current.user_id)
        return _view(db, event, request.app.state.settings)


@router.patch("/{event_id}/facts", response_model=EventView)
def correct_event_facts(
    event_id: str,
    body: UpdateFacts,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    with request.app.state.database.sessions.begin() as db:
        event = event_by_id(db, event_id)
        before = material_event_snapshot(event)
        update_facts(
            event,
            body.expected_revision,
            [value.model_dump() for value in body.changes],
            current.user_id,
        )
        invalidate_material_event_approvals(db, event, before, current.user_id)
        return _view(db, event, request.app.state.settings)


@router.post("/{event_id}/description-suggestion", response_model=DescriptionSuggestionResponse)
def suggest_event_description(
    event_id: str,
    body: DescriptionSuggestionRequest,
    request: Request,
):
    with request.app.state.database.sessions() as db:
        event = event_by_id(db, event_id)
        sources = event_sources(db, event.id)
        source_notes = "\n\n".join(
            source.original_text[:3000]
            for source in sources
            if source.kind == "TEXT" and source.original_text
        )[:9000]
        facts = event.facts or {}
        context = {
            "title": event.title,
            "topic": facts.get("topic"),
            "host": (facts.get("people") or {}).get("host"),
            "family_label": facts.get("family_label"),
            "audience": facts.get("audience"),
            "purpose": facts.get("purpose"),
            "date_and_time": {
                "local_start": facts.get("schedule_local_start"),
                "local_end": facts.get("schedule_local_end"),
                "timezone": event.timezone,
            },
            "delivery": event.delivery,
            "location": facts.get("location"),
            "registration": facts.get("registration"),
            "existing_description": (facts.get("content_facts") or {}).get("description"),
            "description_to_refine": body.current_description,
            "source_notes": source_notes,
        }
    try:
        description = request.app.state.description_suggester.suggest(
            context, tone=body.tone, instruction=body.instruction
        )
    except ValueError:
        raise HTTPException(503, "DESCRIPTION_SUGGESTION_UNAVAILABLE") from None
    return {"description": description}


@router.post("/{event_id}/clarifications/{question_id}", response_model=EventView)
def resolve_clarification(
    event_id: str,
    question_id: str,
    body: AnswerClarification,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    invalid_code = None
    with request.app.state.database.sessions.begin() as db:
        event = event_by_id(db, event_id)
        before = material_event_snapshot(event)
        try:
            with db.begin_nested():
                answer_clarification(
                    event, question_id, body.expected_revision, body.answer, current.user_id
                )
        except HTTPException as error:
            if error.status_code != 422:
                raise
            mark_invalid_answer(event, question_id, str(error.detail))
            invalid_code = str(error.detail)
        if invalid_code is None:
            invalidate_material_event_approvals(db, event, before, current.user_id)
        response = _view(db, event, request.app.state.settings)
    if invalid_code:
        raise HTTPException(422, invalid_code)
    return response


@router.get("/{event_id}/sources/{source_id}/download")
def download_source(event_id: str, source_id: str, request: Request):
    with request.app.state.database.sessions() as db:
        event = event_by_id(db, event_id)
        try:
            source_uuid = uuid.UUID(source_id)
        except ValueError:
            raise HTTPException(404, "SOURCE_NOT_FOUND") from None
        source = db.scalar(
            select(SourceMaterial).where(
                SourceMaterial.id == source_uuid, SourceMaterial.event_id == event.id
            )
        )
        if source is None:
            raise HTTPException(404, "SOURCE_NOT_FOUND")
        if source.kind == "TEXT":
            return Response(
                source.original_text,
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="event-notes.txt"'},
            )
        path = resolve_storage_path(
            request.app.state.settings.private_storage_dir, source.storage_key
        )
        if not path.is_file():
            raise HTTPException(404, "SOURCE_NOT_FOUND")
        return FileResponse(
            path,
            media_type=source.detected_type,
            filename=safe_display_name(source.original_name),
            content_disposition_type="attachment",
        )
