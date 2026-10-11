"""Application-controlled Event facts, intake proposals and clarification rules."""

import copy
import hashlib
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import now
from app.extraction import (
    OUTPUT_SCHEMA_VERSION,
    PROMPT_VERSION,
    ExtractionFailure,
    ExtractionInput,
    FactProposal,
)
from app.file_storage import MAX_EVENT_BYTES, MAX_TEXT_BYTES, resolve_storage_path
from app.models import Event, SourceMaterial
from app.schedule_input import parse_schedule_answer, parse_source_schedule

SCHEMA_VERSION = 1
MAX_OPEN_QUESTIONS = 3
MAX_EXTRACTION_ATTEMPTS = 20
LOGGER = logging.getLogger("event_marketing.extraction")
DELIVERIES = {"IN_PERSON", "VIRTUAL", "HYBRID"}
FIELD_PATHS = {
    "title",
    "schedule",
    "timezone",
    "delivery",
    "facts.location.venue",
    "facts.location.address",
    "facts.location.street",
    "facts.location.city",
    "facts.location.region",
    "facts.location.postal_code",
    "facts.location.country",
    "facts.location.join_url",
    "facts.location.join_instructions",
    "facts.people.host",
    "facts.family_label",
    "facts.topic",
    "facts.content_facts.description",
    "facts.registration.price_kind",
    "facts.registration.strategy",
}
# Accepted only from an explicit user correction, never from AI extraction proposals.
CORRECTION_ONLY_PATHS = {"facts.registration.capacity"}
MAX_CAPACITY = 1_000_000
TIMEZONE_ALIASES = {
    "mountain time": "America/Denver",
    "pacific time": "America/Los_Angeles",
    "central time": "America/Chicago",
    "eastern time": "America/New_York",
    "utc": "UTC",
}


def initial_facts() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "schedule_local_start": None,
        "schedule_local_end": None,
        "location": {},
        "people": {},
        "registration": {},
        "content_facts": {},
    }


def initial_intake() -> dict:
    return {"schema_version": SCHEMA_VERSION, "proposals": [], "clarifications": []}


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _safe_proposal(
    proposal: FactProposal,
    source: SourceMaterial,
    default_timezone: str = "America/Denver",
) -> dict | None:
    """Treat extractor output as untrusted structured input and keep a strict field allowlist."""
    if proposal.field_path not in FIELD_PATHS or proposal.source_id != source.id:
        return None
    if proposal.confidence not in {"HIGH", "MEDIUM", "LOW"}:
        return None
    value = proposal.candidate_value
    if proposal.field_path == "title":
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 240:
            return None
        value = value.strip()
    elif proposal.field_path == "schedule":
        if not isinstance(value, dict) or set(value) - {"local_start", "timezone", "local_end"}:
            return None
        value = copy.deepcopy(value)
        if value.get("local_start") is not None:
            try:
                parsed = datetime.fromisoformat(value["local_start"])
                if parsed.tzinfo is not None:
                    return None
            except (TypeError, ValueError):
                return None
        if value.get("timezone") is not None:
            try:
                ZoneInfo(value["timezone"])
            except (ZoneInfoNotFoundError, TypeError):
                return None
        if value.get("local_end") is not None:
            try:
                parsed = datetime.fromisoformat(value["local_end"])
                if parsed.tzinfo is not None:
                    return None
            except (TypeError, ValueError):
                return None
        parsed = parse_source_schedule(proposal.short_excerpt, default_timezone)
        if parsed:
            if value.get("local_start") is None and parsed.get("local_start"):
                value["local_start"] = parsed["local_start"]
                value["local_end"] = parsed.get("local_end")
            if not value.get("timezone"):
                value["timezone"] = parsed.get("timezone")
            if not value.get("timezone"):
                value["timezone"] = default_timezone
            value.update(
                {
                    key: parsed[key]
                    for key in (
                        "date_only",
                        "start_time",
                        "end_time",
                        "year_inferred",
                        "past_date",
                        "interpretation_note",
                    )
                    if parsed.get(key) is not None
                }
            )
        elif not value.get("timezone"):
            value["timezone"] = default_timezone
    elif proposal.field_path == "delivery":
        if not isinstance(value, str) or value not in DELIVERIES:
            return None
    elif proposal.field_path == "timezone":
        if not isinstance(value, str):
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            return None
    elif proposal.field_path == "facts.location.join_url":
        if not isinstance(value, str) or len(value) > 500:
            return None
        try:
            parsed_url = urlsplit(value)
        except ValueError:
            return None
        if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username:
            return None
    elif proposal.field_path == "facts.registration.price_kind":
        if not isinstance(value, str) or value not in {"FREE", "PAID"}:
            return None
    elif proposal.field_path == "facts.registration.strategy":
        if not isinstance(value, str) or value not in {
            "PROVIDED_URL",
            "DESTINATION_CREATED",
            "NO_REGISTRATION",
            "UNDECIDED",
        }:
            return None
    else:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 500:
            return None
        value = value.strip()
    return {
        "id": uuid.uuid4().hex,
        "field_path": proposal.field_path,
        "candidate_value": _json_value(value),
        "confidence": proposal.confidence,
        "source_id": str(source.id),
        "locator": str(proposal.locator)[:160],
        "short_excerpt": str(proposal.short_excerpt)[:240],
        "uncertainty_reason": _schedule_uncertainty(proposal, value)
        if proposal.field_path == "schedule"
        else (str(proposal.uncertainty_reason)[:240] if proposal.uncertainty_reason else None),
        "status": "PENDING",
    }


def _schedule_uncertainty(proposal: FactProposal, value: dict) -> str | None:
    reasons = [
        str(reason).strip()
        for reason in (proposal.uncertainty_reason, value.get("interpretation_note"))
        if reason and str(reason).strip()
    ]
    return " ".join(dict.fromkeys(reasons))[:240] or None


def _source_input(source: SourceMaterial, storage_dir: Path) -> ExtractionInput:
    if source.kind == "TEXT":
        return ExtractionInput(source.id, source.original_text)
    path = resolve_storage_path(storage_dir, source.storage_key)
    try:
        data = path.read_bytes()
    except OSError:
        raise ExtractionFailure("SOURCE_UNAVAILABLE", False) from None
    if len(data) != source.byte_count or hashlib.sha256(data).hexdigest() != source.sha256:
        raise ExtractionFailure("SOURCE_INTEGRITY", False)
    if source.detected_type in {"text/plain", "text/markdown"}:
        try:
            text = data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            raise ExtractionFailure("SOURCE_INVALID", False) from None
        return ExtractionInput(source.id, text, mime_type=source.detected_type)
    return ExtractionInput(source.id, None, data, source.detected_type)


def _append_bounded_attempts(extraction: dict, attempt: dict) -> dict:
    attempts = [*extraction.get("attempts", []), attempt]
    dropped = max(0, len(attempts) - MAX_EXTRACTION_ATTEMPTS)
    return {
        "schema_version": SCHEMA_VERSION,
        "extractor_type": extraction.get("extractor_type"),
        "proposals": extraction.get("proposals", []),
        "latest_attempt_id": attempt["id"],
        "attempts": attempts[-MAX_EXTRACTION_ATTEMPTS:],
        "pruned_attempt_count": extraction.get("pruned_attempt_count", 0) + dropped,
    }


def run_source_extraction(
    sessions,
    storage_dir: Path,
    event_id,
    source_id,
    extractor,
    default_timezone: str = "America/Denver",
):
    """Persist attempt start, call provider outside a DB transaction, then persist outcome."""
    with sessions() as db:
        event = db.scalar(select(Event).where(Event.id == event_id))
        source = db.scalar(
            select(SourceMaterial).where(
                SourceMaterial.id == source_id, SourceMaterial.event_id == event_id
            )
        )
        if event is None or source is None:
            return

    attempt_id = uuid.uuid4().hex
    started_at = now()
    attempt = {
        "id": attempt_id,
        "event_id": str(event_id),
        "source_id": str(source_id),
        "extractor_type": extractor.extractor_type,
        "model_id": extractor.model_id,
        "prompt_version": PROMPT_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "status": "RUNNING",
        "started_at": started_at.isoformat(),
    }
    with sessions.begin() as db:
        source = db.scalar(
            select(SourceMaterial)
            .where(SourceMaterial.id == source_id, SourceMaterial.event_id == event_id)
            .with_for_update()
        )
        if source is None:
            return
        extraction = copy.deepcopy(source.extraction or {})
        latest_id = extraction.get("latest_attempt_id")
        latest = next(
            (value for value in extraction.get("attempts", []) if value.get("id") == latest_id),
            None,
        )
        if source.processing_status == "EXTRACTING":
            if latest and latest.get("status") == "RUNNING":
                try:
                    stale = now() - datetime.fromisoformat(latest["started_at"]) > timedelta(
                        minutes=3
                    )
                except (KeyError, ValueError, TypeError):
                    stale = True
                if not stale:
                    return
                latest.update(
                    status="INTERRUPTED",
                    completed_at=now().isoformat(),
                    error_category="STALE_ATTEMPT",
                    retryable=True,
                )
                extraction["attempts"] = [
                    value if value.get("id") != latest_id else latest
                    for value in extraction.get("attempts", [])
                ]
            else:
                return
        elif source.processing_status == "EXTRACTION_FAILED":
            if not (
                latest and latest.get("status") == "FAILED" and latest.get("retryable") is True
            ):
                return
        elif source.processing_status not in {"PENDING", "STORED_PENDING_EXTRACTION"}:
            return
        source.processing_status = "EXTRACTING"
        extraction = _append_bounded_attempts(extraction, attempt)
        extraction["extractor_type"] = extractor.extractor_type
        source.extraction = extraction

    started_clock = time.monotonic()
    try:
        with sessions() as db:
            source = db.scalar(
                select(SourceMaterial).where(
                    SourceMaterial.id == source_id, SourceMaterial.event_id == event_id
                )
            )
            if source is None:
                raise ExtractionFailure("SOURCE_UNAVAILABLE", False)
            material = _source_input(source, storage_dir)
        result = extractor.extract(material)
        validated = []
        with sessions.begin() as db:
            event = db.scalar(select(Event).where(Event.id == event_id).with_for_update())
            source = db.scalar(
                select(SourceMaterial)
                .where(SourceMaterial.id == source_id, SourceMaterial.event_id == event_id)
                .with_for_update()
            )
            if event is None or source is None:
                return
            if source.extraction.get("latest_attempt_id") != attempt_id:
                return
            for proposal in result.proposals:
                safe = _safe_proposal(proposal, source, default_timezone)
                if safe:
                    validated.append(safe)
            intake = copy.deepcopy(event.intake_proposals or initial_intake())
            for proposal in intake.setdefault("proposals", []):
                if (
                    proposal.get("source_id") == str(source.id)
                    and proposal.get("status") == "PENDING"
                ):
                    proposal["status"] = "SUPERSEDED"
            intake["proposals"].extend(validated)
            if validated:
                for question in intake.setdefault("clarifications", []):
                    if question.get("kind") == "MISSING" and question.get("status") == "OPEN":
                        question["status"] = "DEFERRED"
            event.intake_proposals = intake
            event.updated_at = now()
            completed_at = now()
            attempt.update(
                status="SUCCEEDED",
                completed_at=completed_at.isoformat(),
                duration_ms=round((time.monotonic() - started_clock) * 1000),
                provider_response_id=result.provider_response_id,
            )
            current = copy.deepcopy(source.extraction or {})
            current["extractor_type"] = result.extractor_type
            current["proposals"] = validated
            current["attempts"] = [
                attempt if value.get("id") == attempt_id else value
                for value in current.get("attempts", [])
            ]
            source.extraction = current
            source.processing_status = "PROPOSALS_AVAILABLE" if validated else "NO_FACTS_FOUND"
            if not any(
                proposal.get("status") == "PENDING" for proposal in intake.get("proposals", [])
            ):
                recompute_clarifications(event)
    except ExtractionFailure as failure:
        _record_extraction_failure(
            sessions, event_id, source_id, attempt_id, failure, started_clock
        )
    except Exception:
        LOGGER.warning(
            "extraction_failed_unexpectedly event_id=%s source_id=%s", event_id, source_id
        )
        failure = ExtractionFailure("EXTRACTION_FAILED", False)
        _record_extraction_failure(
            sessions, event_id, source_id, attempt_id, failure, started_clock
        )


def _record_extraction_failure(sessions, event_id, source_id, attempt_id, failure, started_clock):
    completed = now()
    with sessions.begin() as db:
        event = db.scalar(select(Event).where(Event.id == event_id).with_for_update())
        source = db.scalar(
            select(SourceMaterial)
            .where(SourceMaterial.id == source_id, SourceMaterial.event_id == event_id)
            .with_for_update()
        )
        if source is None or source.extraction.get("latest_attempt_id") != attempt_id:
            return
        extraction = copy.deepcopy(source.extraction or {})
        attempts = extraction.get("attempts", [])
        for attempt in attempts:
            if attempt.get("id") == attempt_id:
                attempt.update(
                    status="FAILED",
                    completed_at=completed.isoformat(),
                    duration_ms=round((time.monotonic() - started_clock) * 1000),
                    error_category=failure.category,
                    retryable=failure.retryable,
                )
        extraction["attempts"] = attempts
        source.extraction = extraction
        source.processing_status = "EXTRACTION_FAILED"
        if event is not None:
            recompute_clarifications(event)
        LOGGER.warning(
            "event_extraction_failed event_id=%s source_id=%s category=%s retryable=%s",
            event_id,
            source_id,
            failure.category,
            failure.retryable,
        )


def _timezone(value: str) -> tuple[str, ZoneInfo]:
    key = value.strip()
    canonical = TIMEZONE_ALIASES.get(key.casefold(), key)
    try:
        return canonical, ZoneInfo(canonical)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "TIMEZONE_INVALID") from None


def _set_local_schedule(event: Event, local_start: str, local_end: str | None = None) -> bool:
    try:
        start = datetime.fromisoformat(local_start)
        end = datetime.fromisoformat(local_end) if local_end else None
    except (TypeError, ValueError):
        raise HTTPException(422, "SCHEDULE_INVALID") from None
    if start.tzinfo is not None or (end and end.tzinfo is not None):
        raise HTTPException(422, "SCHEDULE_INVALID")
    if end and end <= start:
        raise HTTPException(422, "SCHEDULE_INVALID")
    facts = copy.deepcopy(event.facts or initial_facts())
    facts["schedule_local_start"] = start.isoformat()
    facts["schedule_local_end"] = end.isoformat() if end else None
    event.facts = facts
    if not event.timezone:
        event.starts_at = event.ends_at = None
        return True
    return _apply_timezone(event, event.timezone)


def _apply_timezone(event: Event, zone_name: str) -> bool:
    canonical, zone = _timezone(zone_name)
    facts = copy.deepcopy(event.facts or initial_facts())
    local_value = facts.get("schedule_local_start")
    if not local_value:
        changed = event.timezone != canonical
        event.timezone = canonical
        return changed
    local = datetime.fromisoformat(local_value)
    first = local.replace(tzinfo=zone, fold=0)
    second = local.replace(tzinfo=zone, fold=1)
    roundtrip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if roundtrip != local:
        raise HTTPException(422, "SCHEDULE_TIME_DOES_NOT_EXIST")
    if first.utcoffset() != second.utcoffset():
        raise HTTPException(422, "SCHEDULE_TIME_AMBIGUOUS")
    local_end = facts.get("schedule_local_end")
    end_aware = None
    if local_end:
        end = datetime.fromisoformat(local_end)
        end_aware = end.replace(tzinfo=zone)
        if end_aware.astimezone(UTC) <= first.astimezone(UTC):
            raise HTTPException(422, "SCHEDULE_INVALID")
    changed = event.timezone != canonical or event.starts_at != first.astimezone(UTC)
    event.timezone = canonical
    event.starts_at = first.astimezone(UTC)
    event.ends_at = end_aware.astimezone(UTC) if end_aware else None
    return changed


def _path_value(event: Event, path: str):
    if path == "title":
        return event.title
    if path == "schedule":
        facts = event.facts or {}
        if facts.get("schedule_local_start"):
            return {
                "local_start": facts["schedule_local_start"],
                "timezone": event.timezone,
                "local_end": facts.get("schedule_local_end"),
            }
        return None
    if path == "timezone":
        return event.timezone
    if path == "delivery":
        return event.delivery
    current = event.facts or {}
    for part in path.split(".")[1:]:
        current = current.get(part, {}) if isinstance(current, dict) else {}
    return current if current != {} else None


def _set_path(event: Event, path: str, value, timezone: str | None = None) -> bool:
    if path not in FIELD_PATHS | CORRECTION_ONLY_PATHS:
        raise HTTPException(422, "FACT_FIELD_NOT_SUPPORTED")
    if path == "facts.registration.capacity":
        text = value.strip() if isinstance(value, str) else ""
        if not text.isdigit() or not 1 <= int(text) <= MAX_CAPACITY:
            raise HTTPException(422, "CAPACITY_INVALID")
        facts = copy.deepcopy(event.facts or initial_facts())
        registration = facts.setdefault("registration", {})
        changed = registration.get("capacity") != int(text)
        registration["capacity"] = int(text)
        event.facts = facts
        return changed
    if path == "title":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 240:
            raise HTTPException(422, "TITLE_INVALID")
        normalized = value.strip()
        changed = normalized != event.title
        event.title = normalized
        return changed
    if path == "schedule":
        if isinstance(value, dict):
            local_start = value.get("local_start")
            local_end = value.get("local_end")
            timezone = value.get("timezone") or timezone
            if not local_start:
                raise HTTPException(422, "SCHEDULE_REQUIRES_YEAR_AND_TIME")
        else:
            local_start, local_end = value, None
        before = (
            event.starts_at,
            event.ends_at,
            event.timezone,
            (event.facts or {}).get("schedule_local_start"),
        )
        if timezone:
            canonical, _ = _timezone(timezone)
            event.timezone = canonical
        _set_local_schedule(event, local_start, local_end)
        after = (
            event.starts_at,
            event.ends_at,
            event.timezone,
            (event.facts or {}).get("schedule_local_start"),
        )
        return before != after
    if path == "timezone":
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(422, "TIMEZONE_INVALID")
        return _apply_timezone(event, value)
    if path == "delivery":
        normalized = (
            value.strip().upper().replace("-", "_").replace(" ", "_")
            if isinstance(value, str)
            else ""
        )
        if normalized not in DELIVERIES:
            raise HTTPException(422, "DELIVERY_INVALID")
        changed = normalized != event.delivery
        event.delivery = normalized
        return changed
    parts = path.split(".")[1:]
    maximum = (
        2048
        if path == "facts.location.join_url"
        else 5000
        if path == "facts.content_facts.description"
        else 500
    )
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise HTTPException(422, "FACT_VALUE_INVALID")
    facts = copy.deepcopy(event.facts or initial_facts())
    if path == "facts.location.join_url":
        try:
            parsed = urlsplit(value.strip())
        except ValueError:
            raise HTTPException(422, "URL_INVALID") from None
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise HTTPException(422, "URL_INVALID")
    node = facts
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    normalized = value.strip()
    changed = node.get(parts[-1]) != normalized
    node[parts[-1]] = normalized
    event.facts = facts
    return changed


def _evidence(
    event: Event, path: str, source_ids: list[str], actor_id, note: str | None = None
) -> None:
    evidence = copy.deepcopy(event.fact_evidence or {})
    existing = evidence.get(path, {})
    supporting = list(dict.fromkeys(existing.get("source_ids", []) + source_ids))
    evidence[path] = {
        "status": "CONFIRMED",
        "source_ids": supporting,
        "accepted_by": str(actor_id),
        "accepted_at": now().isoformat(),
        "note": note,
    }
    event.fact_evidence = evidence


def _timezone_label(value: str | None) -> str | None:
    aliases = {
        "America/Denver": "Mountain Time",
        "America/Los_Angeles": "Pacific Time",
        "America/Chicago": "Central Time",
        "America/New_York": "Eastern Time",
        "UTC": "UTC",
    }
    return aliases.get(value, value) if value else None


def _candidate_label(path: str, value) -> str:
    if path == "schedule" and isinstance(value, dict):
        local = value.get("local_start")
        if local:
            try:
                parsed = datetime.fromisoformat(local)
                label = (
                    parsed.strftime("%B %d, %Y at %I:%M %p")
                    .replace(" 0", " ")
                    .replace(" at 0", " at ")
                )
            except (ValueError, OSError):
                label = local
            zone = _timezone_label(value.get("timezone"))
            warning = " · Past date—please confirm" if value.get("past_date") else ""
            return f"{label}{' · ' + zone if zone else ''}{warning}"
        if value.get("date_only"):
            try:
                day = datetime.fromisoformat(value["date_only"]).date()
                label = f"{day.strftime('%B')} {day.day}, {day.year}"
            except (TypeError, ValueError):
                label = "Date found"
            if value.get("start_time"):
                label += f" · {value['start_time']}"
            else:
                label += " · start time needed"
            if value.get("past_date"):
                label += " · Past date—please confirm"
            return label
        if value.get("start_time"):
            return f"Start time found: {value['start_time']} · date needed"
        return "Date and start time need your input"
    if path == "delivery":
        return {"IN_PERSON": "In person", "VIRTUAL": "Virtual", "HYBRID": "Hybrid"}.get(
            value, str(value)
        )
    if isinstance(value, (dict, list)):
        return "Review this detail"
    return str(value)


def _question_text(kind: str, path: str) -> str:
    if kind == "CONFLICT":
        return "I found different details for this event. Which one should I use?"
    if kind == "CONFIRMATION_REQUIRED" and path == "schedule":
        return "Please review the suggested date and time."
    return {
        "title": "What should we call this event?",
        "schedule": "When does the event begin?",
        "timezone": "What time zone should we use?",
        "delivery": "How will people attend?",
        "facts.location.venue": "Where will the event take place? Share a venue or address.",
        "facts.location.join_instructions": (
            "How will attendees register or join online? A link or short instruction is enough."
        ),
        "facts.topic": "What is the main topic or purpose of this Event?",
        "facts.people.host": "Who is hosting this Event?",
    }.get(path, "What should attendees know about this Event?")


def _new_question(kind: str, path: str, candidates: list[dict] | None = None) -> dict:
    values = []
    for candidate in candidates or []:
        values.append(
            {
                "label": _candidate_label(path, candidate.get("value")),
                "value": candidate.get("value"),
                "source_id": candidate.get("source_id"),
                "reason": candidate.get("reason"),
            }
        )
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "field_path": path,
        "question": _question_text(kind, path),
        "candidates": values,
        "status": "OPEN",
        "answer": None,
        "created_at": now().isoformat(),
        "resolved_at": None,
        "resolved_by": None,
    }


def _open_questions(event: Event) -> list[dict]:
    return [
        value
        for value in (event.intake_proposals or {}).get("clarifications", [])
        if value.get("status") == "OPEN"
    ]


def _missing(event: Event) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    facts = event.facts or {}
    if not event.title:
        missing.append(("MISSING", "title"))
    if not facts.get("schedule_local_start"):
        missing.append(("MISSING", "schedule"))
    elif not event.timezone or not event.starts_at:
        missing.append(("MISSING", "timezone"))
    if not event.delivery:
        missing.append(("MISSING", "delivery"))
    if event.delivery in {"IN_PERSON", "HYBRID"}:
        location = facts.get("location", {})
        if not location.get("venue") and not location.get("address"):
            missing.append(("MISSING", "facts.location.venue"))
    if event.delivery in {"VIRTUAL", "HYBRID"}:
        location, registration = facts.get("location", {}), facts.get("registration", {})
        if not any(
            [
                location.get("join_url"),
                location.get("join_instructions"),
                registration.get("strategy")
                in {"PROVIDED_URL", "DESTINATION_CREATED", "NO_REGISTRATION"},
            ]
        ):
            missing.append(("MISSING", "facts.location.join_instructions"))
    return missing


def recompute_clarifications(event: Event) -> None:
    intake = copy.deepcopy(event.intake_proposals or initial_intake())
    if any(item.get("status") == "PENDING" for item in intake.get("proposals", [])):
        event.intake_proposals = intake
        return
    clarifications = intake.setdefault("clarifications", [])
    required_paths = {path for _, path in _missing(event)}
    for question in clarifications:
        if (
            question.get("status") == "OPEN"
            and question.get("kind") == "MISSING"
            and question.get("field_path") not in required_paths
        ):
            question.update(
                status="RESOLVED",
                answer="No longer required after the Event details changed.",
                resolved_at=now().isoformat(),
                resolved_by="SYSTEM",
            )
    open_paths = {
        value.get("field_path") for value in clarifications if value.get("status") == "OPEN"
    }
    budget = max(0, MAX_OPEN_QUESTIONS - len(open_paths))
    for kind, path in _missing(event):
        if budget <= 0:
            break
        if path not in open_paths:
            clarifications.append(_new_question(kind, path))
            open_paths.add(path)
            budget -= 1
    event.intake_proposals = intake


def readiness(event: Event) -> dict:
    missing = _missing(event)
    unresolved = _open_questions(event)
    open_conflicts = any(
        value.get("kind") in {"CONFLICT", "CONFIRMATION_REQUIRED", "INVALID"}
        for value in unresolved
    )
    labels = [path for _, path in missing]
    labels.extend(value["field_path"] for value in unresolved if value["kind"] != "MISSING")
    return {
        "ready": not missing and not open_conflicts,
        "missing_fields": list(dict.fromkeys(labels)),
    }


def _resolve_questions(event: Event, path: str, answer: str, actor_id) -> None:
    intake = copy.deepcopy(event.intake_proposals or initial_intake())
    changed = False
    for question in intake.get("clarifications", []):
        if question.get("field_path") == path and question.get("status") == "OPEN":
            question.update(
                status="RESOLVED",
                answer=answer[:1000],
                resolved_at=now().isoformat(),
                resolved_by=str(actor_id),
            )
            changed = True
    if changed:
        event.intake_proposals = intake


def _equivalent(path: str, left, right) -> bool:
    if path == "delivery":
        return str(left).upper().replace("-", "_").replace(" ", "_") == str(right).upper().replace(
            "-", "_"
        ).replace(" ", "_")
    return left == right


def _public_confidence(value) -> str:
    if isinstance(value, str) and value in {"HIGH", "MEDIUM", "LOW"}:
        return value
    # Normalize proposals persisted by the accepted WP1B float-confidence extractor.
    if isinstance(value, (int, float)) and 0 <= value <= 1:
        return "HIGH" if value >= 0.8 else "MEDIUM" if value >= 0.6 else "LOW"
    return "LOW"


def _needs_confirmation(value) -> bool:
    if isinstance(value, str):
        return value != "HIGH"
    return not isinstance(value, (int, float)) or value < 0.8


def accept_proposals(db: Session, event: Event, expected_revision: int, actor_id) -> None:
    if event.revision != expected_revision:
        raise HTTPException(409, "REVISION_CONFLICT")
    intake = copy.deepcopy(event.intake_proposals or initial_intake())
    pending = [value for value in intake.get("proposals", []) if value.get("status") == "PENDING"]
    if not pending:
        raise HTTPException(409, "NO_PENDING_PROPOSALS")
    grouped: dict[str, list[dict]] = {}
    for proposal in pending:
        if proposal.get("field_path") in FIELD_PATHS:
            grouped.setdefault(proposal["field_path"], []).append(proposal)
    changed = False
    generated_conflicts: list[dict] = []
    for path, values in grouped.items():
        candidates = []
        for value in values:
            candidate = {
                "value": value["candidate_value"],
                "source_id": value["source_id"],
                "reason": value.get("uncertainty_reason"),
            }
            if not any(
                _equivalent(path, candidate["value"], prior["value"]) for prior in candidates
            ):
                candidates.append(candidate)
        current = _path_value(event, path)
        if len(candidates) > 1 or (
            current is not None and not _equivalent(path, current, candidates[0]["value"])
        ):
            if current is not None:
                candidates.insert(0, {"value": current, "source_id": None})
            generated_conflicts.append(_new_question("CONFLICT", path, candidates))
            for value in values:
                value["status"] = "CONFLICT"
            continue
        primary = values[0]
        candidate = primary["candidate_value"]
        if _needs_confirmation(primary["confidence"]) or primary.get("uncertainty_reason"):
            generated_conflicts.append(_new_question("CONFIRMATION_REQUIRED", path, candidates))
            for value in values:
                value["status"] = "NEEDS_CONFIRMATION"
            continue
        if current is None:
            if path == "schedule" and not candidate.get("local_start"):
                generated_conflicts.append(_new_question("CONFIRMATION_REQUIRED", path, candidates))
                for value in values:
                    value["status"] = "NEEDS_CONFIRMATION"
                continue
            changed = _set_path(event, path, candidate) or changed
        _evidence(event, path, [value["source_id"] for value in values], actor_id)
        _resolve_questions(event, path, _candidate_label(path, candidate), actor_id)
        for value in values:
            value["status"] = "ACCEPTED"
    intake["proposals"] = [
        next(
            (
                value
                for values in grouped.values()
                for value in values
                if value["id"] == original["id"]
            ),
            original,
        )
        for original in intake.get("proposals", [])
    ]
    existing = intake.setdefault("clarifications", [])
    existing.extend(generated_conflicts)
    event.intake_proposals = intake
    recompute_clarifications(event)
    if changed:
        event.revision += 1
    event.updated_at = now()


def _parse_schedule_answer(
    answer: str, default_timezone: str = "America/Denver"
) -> tuple[str, str | None, str | None]:
    try:
        parsed = parse_schedule_answer(answer, default_timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "SCHEDULE_ANSWER_INVALID") from None
    return parsed["local_start"], parsed.get("timezone"), parsed.get("local_end")


def _answer_value(event: Event, path: str, answer: str):
    raw = answer.strip()
    if not raw or len(raw) > 2000:
        raise HTTPException(422, "ANSWER_INVALID")
    if path == "schedule":
        if raw.isdigit() and len(raw) == 4:
            intake = event.intake_proposals or initial_intake()
            question = next(
                (
                    item
                    for item in intake.get("clarifications", [])
                    if item.get("status") == "OPEN" and item.get("field_path") == "schedule"
                ),
                None,
            )
            candidate = next(
                (
                    item.get("value")
                    for item in (question or {}).get("candidates", [])
                    if isinstance(item.get("value"), dict)
                ),
                None,
            )
            if candidate and candidate.get("date_only") and candidate.get("start_time"):
                date_part = candidate["date_only"]
                date_part = f"{raw}-{date_part[5:]}"
                zone = candidate.get("timezone") or event.timezone or "America/Denver"
                answer = f"{date_part}T{candidate['start_time']}||{zone}"
                raw = answer
        local, timezone, local_end = _parse_schedule_answer(raw, event.timezone or "America/Denver")
        return {
            "local_start": local,
            "timezone": timezone or event.timezone or "America/Denver",
            "local_end": local_end,
        }
    if path == "delivery":
        aliases = {
            "in person": "IN_PERSON",
            "in-person": "IN_PERSON",
            "in_person": "IN_PERSON",
            "virtual": "VIRTUAL",
            "online": "VIRTUAL",
            "hybrid": "HYBRID",
        }
        folded = raw.casefold()
        value = next((candidate for phrase, candidate in aliases.items() if phrase in folded), None)
        value = value or raw.upper().replace("-", "_").replace(" ", "_")
        if value not in DELIVERIES:
            raise HTTPException(422, "DELIVERY_INVALID")
        return value
    if path == "timezone":
        return _timezone(raw)[0]
    if path == "facts.location.join_instructions":
        if raw.casefold() in {"no registration", "no registration required"}:
            return {"registration.strategy": "NO_REGISTRATION"}
        if raw.casefold() in {"event page", "registration page", "destination event page"}:
            return {"registration.strategy": "DESTINATION_CREATED"}
        try:
            scheme = urlsplit(raw).scheme.lower()
        except ValueError:
            raise HTTPException(422, "URL_INVALID") from None
        if scheme == "https":
            return {"facts.location.join_url": raw, "registration.strategy": "PROVIDED_URL"}
    return raw


def answer_clarification(
    event: Event, question_id: str, expected_revision: int, answer: str, actor_id
) -> None:
    if event.revision != expected_revision:
        raise HTTPException(409, "REVISION_CONFLICT")
    intake = copy.deepcopy(event.intake_proposals or initial_intake())
    question = next(
        (
            item
            for item in intake.get("clarifications", [])
            if item.get("id") == question_id and item.get("status") == "OPEN"
        ),
        None,
    )
    if question is None:
        raise HTTPException(404, "CLARIFICATION_NOT_FOUND")
    path = question["field_path"]
    value = _answer_value(event, path, answer)
    changed = False
    if isinstance(value, dict) and path == "facts.location.join_instructions":
        for resolved_path, resolved_value in value.items():
            changed = _set_path(event, resolved_path, resolved_value) or changed
            _evidence(event, resolved_path, [], actor_id, "Resolved during clarification")
            _resolve_questions(event, resolved_path, answer, actor_id)
    else:
        changed = _set_path(event, path, value) or changed
        _evidence(event, path, [], actor_id, "Resolved during clarification")
    if path == "schedule" and isinstance(value, dict) and value.get("timezone"):
        changed = _set_path(event, "timezone", value["timezone"]) or changed
    _resolve_questions(event, path, answer, actor_id)
    if changed:
        event.revision += 1
    event.updated_at = now()
    recompute_clarifications(event)


def mark_invalid_answer(event: Event, question_id: str, code: str) -> None:
    messages = {
        "DELIVERY_INVALID": "Choose in person, virtual, or hybrid.",
        "SCHEDULE_ANSWER_INVALID": "Include the complete date, year, and start time.",
        "SCHEDULE_REQUIRES_YEAR_AND_TIME": "Include the complete date, year, and start time.",
        "SCHEDULE_TIME_AMBIGUOUS": "That time occurs twice. Choose another start time.",
        "SCHEDULE_TIME_DOES_NOT_EXIST": "That time does not exist in this zone. Check it.",
        "TIMEZONE_INVALID": "Enter a recognized time zone, such as America/Denver.",
        "FACT_VALUE_INVALID": "Enter a usable detail and try again.",
        "URL_INVALID": "Use a complete HTTPS link.",
    }
    intake = copy.deepcopy(event.intake_proposals or initial_intake())
    question = next(
        (
            item
            for item in intake.get("clarifications", [])
            if item.get("id") == question_id and item.get("status") == "OPEN"
        ),
        None,
    )
    if question is None:
        return
    question["kind"] = "INVALID"
    question["validation_message"] = messages.get(
        code, "That answer needs a correction. Please try again."
    )
    event.intake_proposals = intake
    event.updated_at = now()


def update_facts(event: Event, expected_revision: int, changes: list[dict], actor_id) -> None:
    if event.revision != expected_revision:
        raise HTTPException(409, "REVISION_CONFLICT")
    if not changes or len(changes) > 8:
        raise HTTPException(422, "FACT_CHANGES_INVALID")
    changed = False
    for item in changes:
        path = item["field_path"]
        value = item["value"]
        if path == "schedule" and value in (None, ""):
            raise HTTPException(422, "SCHEDULE_INVALID")
        changed = _set_path(event, path, value) or changed
        _evidence(event, path, [], actor_id, "Corrected by user")
        _resolve_questions(event, path, str(value), actor_id)
    if changed:
        event.revision += 1
    event.updated_at = now()
    recompute_clarifications(event)


def event_readiness(event: Event) -> dict:
    result = readiness(event)
    result["open_question_count"] = len(_open_questions(event))
    return result


def current_state(event: Event) -> str:
    proposals = (event.intake_proposals or {}).get("proposals", [])
    if any(value.get("status") == "PENDING" for value in proposals):
        return "NEEDS_REVIEW"
    if readiness(event)["ready"]:
        return "READY"
    return "NEEDS_DETAILS"


def public_facts(event: Event) -> dict:
    facts = copy.deepcopy(event.facts or initial_facts())
    return {
        "family_label": facts.get("family_label"),
        "topic": facts.get("topic"),
        "schedule_local_start": facts.get("schedule_local_start"),
        "schedule_local_end": facts.get("schedule_local_end"),
        "location": facts.get("location", {}),
        "people": facts.get("people", {}),
        "registration": facts.get("registration", {}),
        "content_facts": facts.get("content_facts", {}),
    }


def event_view(event: Event, sources: list[SourceMaterial]) -> dict:
    intake = event.intake_proposals or initial_intake()
    proposals = [
        {
            key: (_public_confidence(value) if key == "confidence" else value)
            for key, value in item.items()
            if key != "status"
        }
        | {"status": item.get("status")}
        for item in intake.get("proposals", [])
    ]
    clarifications = [
        {
            "id": value["id"],
            "kind": value["kind"],
            "field_path": value["field_path"],
            "question": value["question"],
            "candidates": value.get("candidates", []),
            "status": value["status"],
            "answer": value.get("answer"),
            "validation_message": value.get("validation_message"),
            "created_at": value["created_at"],
            "resolved_at": value.get("resolved_at"),
        }
        for value in intake.get("clarifications", [])
    ]
    return {
        "id": str(event.id),
        "revision": event.revision,
        "title": event.title,
        "starts_at": event.starts_at.isoformat() if event.starts_at else None,
        "ends_at": event.ends_at.isoformat() if event.ends_at else None,
        "timezone": event.timezone,
        "delivery": event.delivery,
        "facts": public_facts(event),
        "proposals": proposals,
        "clarifications": clarifications,
        "readiness": event_readiness(event),
        "state": current_state(event),
        "created_at": event.created_at.isoformat(),
        "updated_at": event.updated_at.isoformat(),
        "sources": [
            {
                "id": str(source.id),
                "kind": source.kind,
                "filename": source.original_name,
                "content_type": source.detected_type,
                "byte_count": source.byte_count,
                "processing_status": source.processing_status,
                "can_retry_extraction": can_retry_source(source),
                "created_at": source.created_at.isoformat(),
            }
            for source in sources
        ],
    }


def list_events(db: Session) -> list[dict]:
    rows = db.scalars(select(Event).order_by(Event.updated_at.desc(), Event.id)).all()
    return [
        {
            "id": str(event.id),
            "revision": event.revision,
            "title": event.title or "Untitled event",
            "starts_at": event.starts_at.isoformat() if event.starts_at else None,
            "timezone": event.timezone,
            "delivery": event.delivery,
            "state": current_state(event),
            "readiness": event_readiness(event),
            "updated_at": event.updated_at.isoformat(),
        }
        for event in rows
    ]


def event_sources(db: Session, event_id) -> list[SourceMaterial]:
    return db.scalars(
        select(SourceMaterial)
        .where(SourceMaterial.event_id == event_id)
        .order_by(SourceMaterial.created_at)
    ).all()


def can_retry_source(source: SourceMaterial) -> bool:
    latest_id = (source.extraction or {}).get("latest_attempt_id")
    latest = next(
        (
            value
            for value in (source.extraction or {}).get("attempts", [])
            if value.get("id") == latest_id
        ),
        None,
    )
    if not latest:
        return False
    if (
        source.processing_status == "EXTRACTION_FAILED"
        and latest.get("status") == "FAILED"
        and latest.get("retryable") is True
    ):
        return True
    if source.processing_status == "EXTRACTING" and latest.get("status") == "RUNNING":
        try:
            started = datetime.fromisoformat(latest["started_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            return now() - started > timedelta(minutes=3)
        except (KeyError, TypeError, ValueError):
            return True
    return False


def event_by_id(db: Session, event_id) -> Event:
    from uuid import UUID

    try:
        value = UUID(str(event_id))
    except ValueError:
        raise HTTPException(404, "EVENT_NOT_FOUND") from None
    event = db.scalar(select(Event).where(Event.id == value).with_for_update())
    if event is None:
        raise HTTPException(404, "EVENT_NOT_FOUND")
    return event


def enforce_source_quota(db: Session, event_id, byte_count: int) -> None:
    current = db.scalar(
        select(func.coalesce(func.sum(SourceMaterial.byte_count), 0)).where(
            SourceMaterial.event_id == event_id
        )
    )
    if current + byte_count > MAX_EVENT_BYTES:
        raise HTTPException(413, "EVENT_SOURCE_LIMIT_EXCEEDED")


def create_source_text(
    db: Session, event: Event, actor_id, text: str, method: str
) -> SourceMaterial:
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise HTTPException(413, "TEXT_TOO_LARGE")
    if not text.strip():
        raise HTTPException(422, "TEXT_REQUIRED")
    enforce_source_quota(db, event.id, len(encoded))
    import hashlib
    from uuid import uuid4

    source = SourceMaterial(
        id=uuid4(),
        event_id=event.id,
        kind="TEXT",
        original_name=None,
        storage_key=None,
        original_text=text,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
        detected_type="text/plain; charset=utf-8",
        input_method=method,
        processing_status="PENDING",
        extraction={},
        supplied_by=actor_id,
        created_at=now(),
    )
    db.add(source)
    db.flush()
    return source
