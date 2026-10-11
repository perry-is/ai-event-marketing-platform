"""Deterministic Campaign generation and immutable version workflow."""

import copy
import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.event_service import event_readiness, public_facts
from app.models import Campaign, CampaignVersion, CampaignVersionAsset, Event, GeneratedAsset

LOGGER = logging.getLogger("event_marketing.campaign")
GENERATOR_ID = "deterministic-template"
GENERATOR_VERSION = "1"
TEMPLATE_VERSION = "event-marketing-campaign-v1"
SNAPSHOT_SCHEMA_VERSION = 1

Surface = Literal[
    "EVENTBRITE",
    "MEETUP",
    "LINKEDIN_ORGANIZATION",
    "LINKEDIN_PERSONAL",
    "INSTAGRAM",
    "FACEBOOK_GROUPS",
]
SUPPORTED_SURFACES: tuple[Surface, ...] = (
    "EVENTBRITE",
    "MEETUP",
    "LINKEDIN_ORGANIZATION",
    "LINKEDIN_PERSONAL",
    "INSTAGRAM",
    "FACEBOOK_GROUPS",
)


class CampaignContent(BaseModel):
    """The semantic pieces shown in review; this is not an API payload."""

    model_config = ConfigDict(extra="forbid", strict=True)

    marketing_headline: StrictStr = Field(min_length=1, max_length=240)
    short_summary: StrictStr = Field(min_length=1, max_length=500)
    general_description: StrictStr = Field(min_length=1, max_length=5000)
    attendee_experience: StrictStr | None = Field(max_length=1000)
    eventbrite_description: StrictStr | None = Field(max_length=5000)
    meetup_description: StrictStr | None = Field(max_length=5000)
    linkedin_organization_post: StrictStr | None = Field(max_length=3000)
    linkedin_personal_post: StrictStr | None = Field(max_length=3000)
    instagram_caption: StrictStr | None = Field(max_length=2200)
    facebook_groups_post: StrictStr | None = Field(max_length=3000)
    creative_direction: StrictStr = Field(min_length=1, max_length=1200)

    _generation_metadata: dict = PrivateAttr(default_factory=dict)

    @property
    def generation_metadata(self) -> dict:
        return copy.deepcopy(self._generation_metadata)


class CampaignGenerator(Protocol):
    def generate(
        self, event_snapshot: dict, selected_surfaces: list[Surface]
    ) -> CampaignContent: ...


class CampaignRevisionService(Protocol):
    def revise(
        self,
        current: CampaignContent,
        event_snapshot: dict,
        revision_request: str,
        selected_surfaces: list[Surface],
    ) -> tuple[CampaignContent, list[str]]: ...


SURFACE_FIELDS: dict[Surface, str] = {
    "EVENTBRITE": "eventbrite_description",
    "MEETUP": "meetup_description",
    "LINKEDIN_ORGANIZATION": "linkedin_organization_post",
    "LINKEDIN_PERSONAL": "linkedin_personal_post",
    "INSTAGRAM": "instagram_caption",
    "FACEBOOK_GROUPS": "facebook_groups_post",
}


def canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def business_event_projection(event_snapshot: dict) -> dict:
    return {key: value for key, value in event_snapshot.items() if key not in {"id", "revision"}}


def campaign_hash_projection(snapshot: dict) -> dict:
    projection = {
        "event_facts": business_event_projection(snapshot["event"]),
        "selected_surfaces": list(snapshot["selected_surfaces"]),
        "content": snapshot["content"],
    }
    graphic = snapshot.get("primary_graphic")
    if graphic is not None:
        projection["primary_graphic"] = {
            key: graphic[key]
            for key in ("asset_id", "role", "sha256", "composition_version", "layout_variant")
        }
    groups = snapshot.get("facebook_group_targets")
    if groups:
        # Absent for versions without selected Groups, so earlier hashes are unchanged.
        from app.group_recommendations import hash_projection

        projection["facebook_group_targets"] = hash_projection(groups)
    return projection


def snapshot_hash(snapshot: dict) -> str:
    canonical = canonical_json(campaign_hash_projection(snapshot)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def material_event_snapshot(event: Event) -> dict:
    return {
        "id": str(event.id),
        "revision": event.revision,
        "title": event.title,
        "starts_at": event.starts_at.isoformat() if event.starts_at else None,
        "ends_at": event.ends_at.isoformat() if event.ends_at else None,
        "timezone": event.timezone,
        "delivery": event.delivery,
        "facts": public_facts(event),
    }


def is_stale(event: Event, version: CampaignVersion) -> bool:
    return business_event_projection(material_event_snapshot(event)) != business_event_projection(
        version.snapshot["event"]
    )


def validate_surfaces(values: list[str]) -> list[Surface]:
    if not values or len(values) > len(SUPPORTED_SURFACES):
        raise HTTPException(422, "CAMPAIGN_SURFACES_REQUIRED")
    if any(not isinstance(value, str) for value in values):
        raise HTTPException(422, "CAMPAIGN_SURFACE_UNSUPPORTED")
    if len(set(values)) != len(values) or any(value not in SUPPORTED_SURFACES for value in values):
        raise HTTPException(422, "CAMPAIGN_SURFACE_UNSUPPORTED")
    return [surface for surface in SUPPORTED_SURFACES if surface in values]


def validate_content(content: CampaignContent, selected_surfaces: list[Surface]) -> CampaignContent:
    try:
        metadata = content.generation_metadata
        validated = CampaignContent.model_validate(content.model_dump(), strict=True)
        validated._generation_metadata = metadata
    except (ValidationError, AttributeError, TypeError):
        raise HTTPException(503, "CAMPAIGN_GENERATION_FAILED") from None
    selected = set(selected_surfaces)
    for surface, field in SURFACE_FIELDS.items():
        value = getattr(validated, field)
        if surface in selected:
            if not value or not value.strip():
                raise HTTPException(503, "CAMPAIGN_GENERATION_FAILED")
        elif value is not None:
            raise HTTPException(503, "CAMPAIGN_GENERATION_FAILED")
    return validated


def _format_datetime(value: str | None, zone_name: str | None) -> str | None:
    if not value:
        return None
    instant = datetime.fromisoformat(value)
    if zone_name:
        instant = instant.astimezone(ZoneInfo(zone_name))
    time_text = instant.strftime("%I:%M %p").lstrip("0")
    return f"{instant.strftime('%A, %B')} {instant.day}, {instant.year} at {time_text}"


def _event_details(event: dict) -> list[str]:
    details: list[str] = []
    start = _format_datetime(event.get("starts_at"), event.get("timezone"))
    end = _format_datetime(event.get("ends_at"), event.get("timezone"))
    if start:
        same_date = bool(end and start and end.rsplit(" at ", 1)[0] == start.rsplit(" at ", 1)[0])
        end_text = end.split(" at ", 1)[-1] if end and same_date else end
        details.append(f"When: {start}" + (f" – {end_text}" if end_text else ""))
    timezone = event.get("timezone")
    if timezone and start:
        details[-1] += f" ({timezone})"
    delivery = event.get("delivery")
    delivery_label = {
        "IN_PERSON": "In person",
        "VIRTUAL": "Virtual",
        "HYBRID": "Hybrid",
    }.get(delivery)
    if delivery_label:
        details.append(f"Format: {delivery_label}")
    facts = event.get("facts") or {}
    location = facts.get("location") or {}
    place = location.get("venue") or location.get("address")
    if place:
        details.append(f"Where: {place}")
        if location.get("address") and location.get("venue"):
            details[-1] += f" · {location['address']}"
    host = (facts.get("people") or {}).get("host")
    if host:
        details.append(f"Host: {host}")
    presenters = (facts.get("people") or {}).get("presenters")
    if presenters:
        details.append(f"Presenters: {presenters}")
    partners = (facts.get("people") or {}).get("partners")
    if partners:
        details.append(f"Partners: {partners}")
    registration = facts.get("registration") or {}
    price_kind = registration.get("price_kind")
    if price_kind == "FREE":
        details.append("Admission: Free")
    elif price_kind == "PAID":
        amount_minor = registration.get("amount_minor")
        currency = registration.get("currency")
        if isinstance(amount_minor, int) and currency:
            details.append(f"Admission: {currency} {amount_minor / 100:.2f}")
        else:
            details.append("Admission: Paid")
    strategy = registration.get("strategy")
    if strategy == "NO_REGISTRATION":
        details.append("Registration is not required.")
    elif strategy == "PROVIDED_URL":
        url = registration.get("url")
        details.append(
            f"Registration: {url}"
            if url
            else "Use the registration details provided for this Event."
        )
    elif strategy == "DESTINATION_CREATED":
        details.append("Registration details will be created for the selected destination.")
    return details


class DeterministicCampaignService:
    """Small, replaceable template generator and rule-driven revision demonstrator."""

    def generate(self, event_snapshot: dict, selected_surfaces: list[Surface]) -> CampaignContent:
        title = event_snapshot.get("title")
        if not title:
            raise ValueError("campaign requires a title")
        facts = event_snapshot.get("facts") or {}
        topic = facts.get("topic")
        summary = (
            f"An Event Marketing gathering focused on {topic}."
            if topic
            else f"An Event Marketing gathering: {title}."
        )
        details = _event_details(event_snapshot)
        detail_block = "Confirmed Event details:\n" + "\n".join(details)
        experience = self._attendee_experience(facts.get("content_facts") or {})
        lead = f"Event Marketing invites you to {title}."
        general_parts = [lead]
        if topic:
            general_parts.append(f"The confirmed topic is {topic}.")
        if experience:
            general_parts.append(experience)
        general_parts.extend(details)
        general = "\n\n".join(general_parts)
        content = CampaignContent(
            marketing_headline=title,
            short_summary=summary,
            general_description=general,
            attendee_experience=experience,
            eventbrite_description=(
                f"{lead}\n\n{summary}\n\n{detail_block}"
                if "EVENTBRITE" in selected_surfaces
                else None
            ),
            meetup_description=(
                f"Join the Event Marketing gathering, {title}.\n\n{summary}\n\n{detail_block}"
                if "MEETUP" in selected_surfaces
                else None
            ),
            linkedin_organization_post=(
                f"{lead}\n\n{summary}\n\n{detail_block}"
                if "LINKEDIN_ORGANIZATION" in selected_surfaces
                else None
            ),
            linkedin_personal_post=(
                f"Sharing an invitation to {title}.\n\n{summary}\n\n{detail_block}"
                if "LINKEDIN_PERSONAL" in selected_surfaces
                else None
            ),
            instagram_caption=(
                f"You're invited: {title}.\n{summary}\n\n{detail_block}"
                if "INSTAGRAM" in selected_surfaces
                else None
            ),
            facebook_groups_post=(
                f"Hello, community. {lead}\n\n{summary}\n\n{detail_block}"
                if "FACEBOOK_GROUPS" in selected_surfaces
                else None
            ),
            creative_direction=(
                "Create a clean, welcoming text-led layout. Keep the exact Event name "
                "and confirmed date, time, format, location, and host prominent. Use only "
                "approved brand assets when they are supplied; do not invent or imitate a logo."
            ),
        )
        return validate_content(content, selected_surfaces)

    @staticmethod
    def _attendee_experience(content_facts: dict) -> str | None:
        for key in ("attendee_experience", "attendee_outcome", "purpose", "outcomes"):
            value = content_facts.get(key)
            if isinstance(value, str) and value.strip():
                return f"What to expect: {value.strip()}"
        return None

    def revise(
        self,
        current: CampaignContent,
        event_snapshot: dict,
        revision_request: str,
        selected_surfaces: list[Surface],
    ) -> tuple[CampaignContent, list[str]]:
        operations = parse_revision_request(revision_request, selected_surfaces)
        values = current.model_dump()
        changed_fields: list[str] = []
        for field, actions in operations.items():
            value = values.get(field)
            if not isinstance(value, str) or not value:
                raise HTTPException(422, "REVISION_NOT_SUPPORTED")
            for action in actions:
                if action == "warmer":
                    value = _make_warmer(value, event_snapshot["title"])
                elif action == "shorter":
                    value = _make_shorter(value, event_snapshot["title"])
                elif action == "energetic":
                    value = _make_energetic(value, event_snapshot["title"])
                elif action == "less_formal":
                    value = f"Let's connect: {event_snapshot['title']}"
            if value != values[field]:
                values[field] = value
                changed_fields.append(field)
        if not changed_fields:
            raise HTTPException(422, "REVISION_NOT_SUPPORTED")
        try:
            revised = CampaignContent.model_validate(values, strict=True)
        except ValidationError:
            raise HTTPException(422, "REVISION_NOT_SUPPORTED") from None
        return validate_content(revised, selected_surfaces), changed_fields


def _make_warmer(text: str, title: str) -> str:
    warmer = text.replace(f"Event Marketing invites you to {title}.", f"Join us for {title}.", 1)
    warmer = warmer.replace(f"Sharing an invitation to {title}.", f"Join us for {title}.", 1)
    if "We'd love to see you there." not in warmer and "We’d love to see you there." not in warmer:
        warmer += "\n\nWe’d love to see you there."
    return warmer


def _make_shorter(text: str, title: str) -> str:
    compact = text.replace(f"Event Marketing invites you to {title}.", f"Join us for {title}.", 1)
    compact = compact.replace(
        f"Join the Event Marketing gathering, {title}.", f"Join us for {title}.", 1
    )
    compact = compact.replace(f"Hello, community. Join us for {title}.", f"Join us for {title}.", 1)
    return compact


def _make_energetic(text: str, title: str) -> str:
    lively = text.replace(
        f"Event Marketing invites you to {title}.", f"Come connect at {title}.", 1
    )
    lively = lively.replace(
        f"Join the Event Marketing gathering, {title}.", f"Come connect at {title}.", 1
    )
    lively = lively.replace(f"Sharing an invitation to {title}.", f"Come along to {title}.", 1)
    lively = lively.replace(f"You're invited: {title}.", f"Come connect at {title}.", 1)
    if "We hope you can join us." not in lively:
        lively += "\n\nWe hope you can join us."
    return lively


def _revision_operations(
    instruction: str, selected_surfaces: list[Surface]
) -> dict[str, list[str]]:
    operations: dict[str, list[str]] = {}
    inherited_targets: list[str] = []
    clauses = [
        clause.strip()
        for clause in re.split(r"\s+and\s+|[;.!?]+", instruction, flags=re.IGNORECASE)
        if clause.strip()
    ]
    for clause in clauses:
        lowered = clause.casefold()
        targets: list[str] = []
        if re.search(r"\blinkedin\b", lowered):
            targets = [
                SURFACE_FIELDS[surface]
                for surface in ("LINKEDIN_ORGANIZATION", "LINKEDIN_PERSONAL")
                if surface in selected_surfaces
            ]
        elif re.search(r"\beventbrite\b", lowered):
            targets = [SURFACE_FIELDS["EVENTBRITE"]] if "EVENTBRITE" in selected_surfaces else []
        elif re.search(r"\bmeetup\b", lowered):
            targets = [SURFACE_FIELDS["MEETUP"]] if "MEETUP" in selected_surfaces else []
        elif re.search(r"\binstagram\b", lowered):
            targets = [SURFACE_FIELDS["INSTAGRAM"]] if "INSTAGRAM" in selected_surfaces else []
        elif re.search(r"facebook\s+groups?", lowered):
            targets = (
                [SURFACE_FIELDS["FACEBOOK_GROUPS"]]
                if "FACEBOOK_GROUPS" in selected_surfaces
                else []
            )
        elif re.search(r"\b(?:marketing\s+)?(?:headline|title)\b", lowered):
            targets = ["marketing_headline"]
        if targets:
            inherited_targets = targets
        else:
            targets = inherited_targets

        action = None
        if re.search(r"\b(?:warmer|warm|friendlier|friendly|approachable)\b", lowered):
            action = "warmer"
        elif re.search(r"\b(?:shorten|shorter|concise|more concise)\b", lowered):
            action = "shorter"
        elif re.search(r"\b(?:energetic|livelier|punchier|more energy)\b", lowered):
            action = "energetic"
        elif re.search(r"\b(?:less formal|more casual|casual)\b", lowered):
            action = "less_formal"
        if not targets or not action:
            raise HTTPException(422, "REVISION_NOT_SUPPORTED")
        for field in targets:
            operations.setdefault(field, []).append(action)
    return operations


_FACT_ACTION = re.compile(
    r"\b(?:change|update|correct|move|reschedule|set)\b.{0,80}\b"
    r"(?:date|time|timezone|time\s+zone|venue|location|address|price|cost|"
    r"registration|host|presenter|speaker)\b|"
    r"\b(?:change|update|correct|set)\b.{0,50}\bevent\s+(?:name|title)\b|"
    r"\b(?:make|change|set|switch)\b.{0,40}\b(?:it|this|the event)\b.{0,25}\b"
    r"(?:virtual|in[- ]person|hybrid|free|paid)\b|"
    r"\b(?:actually|the event is|event is|this is)\b.{0,50}\b"
    r"(?:virtual|in[- ]person|hybrid)\b",
    re.IGNORECASE,
)
_FACT_VALUE_CHANGE = re.compile(
    r"\b(?:change|update|correct|move|reschedule|set|switch|make)\b.{0,100}\b"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:,?\s+20\d{2})?\b|"
    r"\b(?:change|update|correct|set)\b.{0,80}(?:\$\s*\d|\b\d+(?:\.\d{2})?\s*(?:USD|dollars?)\b)",
    re.IGNORECASE,
)


def is_event_fact_revision(instruction: str) -> bool:
    return bool(_FACT_ACTION.search(instruction) or _FACT_VALUE_CHANGE.search(instruction))


def revision_target_fields(instruction: str, selected_surfaces: list[Surface]) -> list[str]:
    """Resolve the allowed edit scope before a model call; unscoped requests may edit all copy."""
    cleaned = instruction.strip()
    if not 3 <= len(cleaned) <= 500:
        raise HTTPException(422, "REVISION_REQUEST_INVALID")
    if is_event_fact_revision(cleaned):
        raise HTTPException(409, "EVENT_CORRECTION_REQUIRED")

    lowered = cleaned.casefold()
    fields: set[str] = set()
    selected = set(selected_surfaces)

    if "linkedin" in lowered:
        personal = bool(re.search(r"\b(?:personal\s+linkedin|linkedin\s+personal)\b", lowered))
        organization = bool(
            re.search(
                r"\b(?:linkedin\s+organization|linkedin\s+org|organization\s+linkedin)\b", lowered
            )
        )
        if personal:
            if "LINKEDIN_PERSONAL" not in selected:
                raise HTTPException(422, "REVISION_NOT_SUPPORTED")
            fields.add("linkedin_personal_post")
        elif organization:
            if "LINKEDIN_ORGANIZATION" not in selected:
                raise HTTPException(422, "REVISION_NOT_SUPPORTED")
            fields.add("linkedin_organization_post")
        else:
            linkedin_fields = []
            if "LINKEDIN_ORGANIZATION" in selected:
                linkedin_fields.append("linkedin_organization_post")
            if "LINKEDIN_PERSONAL" in selected:
                linkedin_fields.append("linkedin_personal_post")
            if not linkedin_fields:
                raise HTTPException(422, "REVISION_NOT_SUPPORTED")
            fields.update(linkedin_fields)

    surface_patterns = {
        "EVENTBRITE": (r"\beventbrite\b", "eventbrite_description"),
        "MEETUP": (r"\bmeetup\b", "meetup_description"),
        "INSTAGRAM": (r"\binstagram\b", "instagram_caption"),
        "FACEBOOK_GROUPS": (r"\bfacebook\s+groups?\b", "facebook_groups_post"),
    }
    for surface, (pattern, field) in surface_patterns.items():
        if re.search(pattern, lowered):
            if surface not in selected:
                raise HTTPException(422, "REVISION_NOT_SUPPORTED")
            fields.add(field)

    shared_targets = (
        (r"\b(?:marketing\s+)?headline\b|\btagline\b", "marketing_headline"),
        (r"\b(?:short\s+)?summary\b", "short_summary"),
        (r"\b(?:general|overall|shared|main)\s+description\b", "general_description"),
        (r"\b(?:attendee\s+experience|attendee\s+value|takeaways?)\b", "attendee_experience"),
        (r"\b(?:creative|visual|graphic)\s+(?:direction|brief)\b", "creative_direction"),
    )
    for pattern, field in shared_targets:
        if re.search(pattern, lowered):
            fields.add(field)

    if not fields:
        return list(CampaignContent.model_fields)
    return [field for field in CampaignContent.model_fields if field in fields]


def parse_revision_request(
    instruction: str, selected_surfaces: list[Surface]
) -> dict[str, list[str]]:
    cleaned = instruction.strip()
    if not 3 <= len(cleaned) <= 500:
        raise HTTPException(422, "REVISION_REQUEST_INVALID")
    if is_event_fact_revision(cleaned):
        raise HTTPException(409, "EVENT_CORRECTION_REQUIRED")
    return _revision_operations(cleaned, selected_surfaces)


def _version_graphic(db: Session, version: CampaignVersion) -> dict | None:
    row = db.execute(
        select(CampaignVersionAsset, GeneratedAsset)
        .join(GeneratedAsset, GeneratedAsset.id == CampaignVersionAsset.asset_id)
        .where(
            CampaignVersionAsset.version_id == version.id,
            CampaignVersionAsset.role == "PRIMARY_GRAPHIC",
        )
    ).one_or_none()
    snapshot_graphic = version.snapshot.get("primary_graphic")
    if row is None:
        if snapshot_graphic is not None:
            raise HTTPException(409, "CAMPAIGN_VERSION_HASH_INVALID")
        return None
    association, asset = row
    expected = {
        "asset_id": str(asset.id),
        "role": association.role,
        "sha256": asset.sha256,
        "composition_version": asset.generation_metadata.get("composition_version"),
        "layout_variant": asset.generation_metadata.get("layout_variant", "STANDARD"),
    }
    if (
        asset.asset_type != "CAMPAIGN_GRAPHIC"
        or asset.campaign_id != version.campaign_id
        or snapshot_graphic != expected
    ):
        raise HTTPException(409, "CAMPAIGN_VERSION_HASH_INVALID")
    return {
        **expected,
        "mime_type": asset.mime_type,
        "width": asset.width,
        "height": asset.height,
        "preview_url": f"/api/campaign-versions/{version.id}/graphic",
    }


def _version_view(db: Session, version: CampaignVersion, current_version_id) -> dict:
    snapshot = copy.deepcopy(version.snapshot)
    content = CampaignContent.model_validate(snapshot["content"], strict=True)
    return {
        "id": str(version.id),
        "version_number": version.version_number,
        "parent_version_id": str(version.parent_version_id) if version.parent_version_id else None,
        "source_event_revision": version.event_revision,
        "event_snapshot": snapshot["event"],
        "selected_surfaces": snapshot["selected_surfaces"],
        "content": content.model_dump(mode="json"),
        "revision_request": version.revision_request,
        "revision_targets": version.revision_targets,
        "generator_id": version.generator_id,
        "generator_version": version.generator_version,
        "content_hash": version.content_hash,
        "primary_graphic": _version_graphic(db, version),
        "facebook_group_targets": [
            {key: item[key] for key in item if key != "decision_id"}
            for item in snapshot.get("facebook_group_targets", [])
        ],
        "created_at": version.created_at.isoformat(),
        "is_current": version.id == current_version_id,
    }


def campaign_view(db: Session, campaign: Campaign, event: Event) -> dict:
    versions = db.scalars(
        select(CampaignVersion)
        .where(CampaignVersion.campaign_id == campaign.id)
        .order_by(CampaignVersion.version_number.desc())
    ).all()
    current = next((value for value in versions if value.id == campaign.current_version_id), None)
    current_view = _version_view(db, current, campaign.current_version_id) if current else None
    return {
        "id": str(campaign.id),
        "event_id": str(campaign.event_id),
        "revision": campaign.revision,
        "state": campaign.state,
        "current_version_id": str(campaign.current_version_id)
        if campaign.current_version_id
        else None,
        "current_version_number": current.version_number if current else None,
        "is_stale": bool(current and is_stale(event, current)),
        "stale_message": (
            "Event details have changed since this campaign was generated."
            if current and is_stale(event, current)
            else None
        ),
        "event_readiness": event_readiness(event),
        "current_version": current_view,
        "versions": [
            {
                "id": str(value.id),
                "version_number": value.version_number,
                "revision_request": value.revision_request,
                "revision_targets": value.revision_targets,
                "created_at": value.created_at.isoformat(),
                "is_current": value.id == campaign.current_version_id,
            }
            for value in versions
        ],
    }


def _find_event(db: Session, event_id, *, lock: bool) -> Event:
    query = select(Event).where(Event.id == event_id)
    if lock:
        query = query.with_for_update()
    event = db.scalar(query)
    if event is None:
        raise HTTPException(404, "EVENT_NOT_FOUND")
    return event


def _find_campaign(db: Session, campaign_id, *, lock: bool = False) -> tuple[Campaign, Event]:
    campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id))
    if campaign is None:
        raise HTTPException(404, "CAMPAIGN_NOT_FOUND")
    event = _find_event(db, campaign.event_id, lock=lock)
    if lock:
        campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
    return campaign, event


def _find_primary(db: Session, event_id) -> Campaign | None:
    return db.scalar(
        select(Campaign)
        .where(Campaign.event_id == event_id)
        .order_by(Campaign.updated_at.desc(), Campaign.id)
        .limit(1)
    )


def get_campaign_for_event(sessions: sessionmaker, event_id) -> dict:
    with sessions() as db:
        event = _find_event(db, event_id, lock=False)
        campaign = _find_primary(db, event.id)
        if campaign is None:
            raise HTTPException(404, "CAMPAIGN_NOT_FOUND")
        return campaign_view(db, campaign, event)


def get_campaign(sessions: sessionmaker, campaign_id) -> dict:
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        return campaign_view(db, campaign, event)


def get_campaign_version(sessions: sessionmaker, campaign_id, version_number: int) -> dict:
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        version = db.scalar(
            select(CampaignVersion).where(
                CampaignVersion.campaign_id == campaign.id,
                CampaignVersion.version_number == version_number,
            )
        )
        if version is None:
            raise HTTPException(404, "CAMPAIGN_VERSION_NOT_FOUND")
        result = campaign_view(db, campaign, event)
        result["viewed_version"] = _version_view(db, version, campaign.current_version_id)
        return result


def _snapshot(event_snapshot: dict, surfaces: list[Surface], content: CampaignContent) -> dict:
    generation = {
        "generator_id": GENERATOR_ID,
        "generator_version": GENERATOR_VERSION,
        "template_version": TEMPLATE_VERSION,
    }
    generation.update(content.generation_metadata)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "event": copy.deepcopy(event_snapshot),
        "selected_surfaces": list(surfaces),
        "content": content.model_dump(mode="json"),
        "generation": generation,
    }


def _create_version(
    db: Session,
    campaign: Campaign,
    event_snapshot: dict,
    content: CampaignContent,
    selected_surfaces: list[Surface],
    actor_id,
    *,
    revision_request: str | None,
    revision_targets: list[str],
    primary_graphic: dict | None = None,
    inherit_primary_graphic: bool = True,
) -> CampaignVersion:
    previous = (
        db.get(CampaignVersion, campaign.current_version_id)
        if campaign.current_version_id
        else None
    )
    version_number = previous.version_number + 1 if previous else 1
    snapshot = _snapshot(event_snapshot, selected_surfaces, content)
    if inherit_primary_graphic and primary_graphic is None and previous is not None:
        primary_graphic = copy.deepcopy(previous.snapshot.get("primary_graphic"))
    if primary_graphic is not None:
        snapshot["primary_graphic"] = copy.deepcopy(primary_graphic)
    from app.group_recommendations import REVISION_TARGET, assemble_group_targets

    # the operator's latest Group decisions inform every successor and are frozen into its snapshot.
    group_targets = assemble_group_targets(
        db, campaign, event_snapshot, snapshot["content"], list(selected_surfaces)
    )
    if group_targets:
        snapshot["facebook_group_targets"] = group_targets
    previous_groups = previous.snapshot.get("facebook_group_targets", []) if previous else []
    if [g["group_record_id"] for g in previous_groups] != [
        g["group_record_id"] for g in group_targets
    ] and REVISION_TARGET not in revision_targets:
        revision_targets = [*revision_targets, REVISION_TARGET]
    canonical_text = canonical_json(snapshot)
    version = CampaignVersion(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        version_number=version_number,
        parent_version_id=previous.id if previous else None,
        event_revision=event_snapshot["revision"],
        snapshot=snapshot,
        canonical_snapshot_text=canonical_text,
        content_hash=snapshot_hash(snapshot),
        generator_id=snapshot["generation"]["generator_id"],
        generator_version=snapshot["generation"]["generator_version"],
        revision_request=revision_request,
        revision_targets=revision_targets,
        created_by=actor_id,
        created_at=datetime.now(UTC),
    )
    db.add(version)
    db.flush()
    if primary_graphic is not None:
        db.add(
            CampaignVersionAsset(
                campaign_id=campaign.id,
                version_id=version.id,
                asset_id=uuid.UUID(primary_graphic["asset_id"]),
                role=primary_graphic["role"],
                created_at=datetime.now(UTC),
            )
        )
        db.flush()
    campaign.current_version_id = version.id
    campaign.state = "REVIEW"
    campaign.revision = campaign.revision + 1 if previous else campaign.revision
    campaign.updated_at = datetime.now(UTC)
    return version


def _check_readiness(event: Event) -> None:
    if not event_readiness(event)["ready"]:
        raise HTTPException(409, "EVENT_NOT_READY")


def _check_generation_inputs(event: Event, captured_snapshot: dict) -> None:
    current = material_event_snapshot(event)
    if business_event_projection(current) != business_event_projection(captured_snapshot):
        raise HTTPException(409, "CAMPAIGN_INPUTS_CHANGED")


def create_or_get_campaign(
    sessions: sessionmaker,
    event_id,
    actor_id,
    selected_surfaces: list[str],
    generator: CampaignGenerator,
) -> dict:
    surfaces = validate_surfaces(selected_surfaces)
    with sessions() as db:
        event = _find_event(db, event_id, lock=False)
        _check_readiness(event)
        campaign = _find_primary(db, event.id)
        if campaign and campaign.current_version_id:
            return campaign_view(db, campaign, event)
        captured_snapshot = material_event_snapshot(event)
        captured_event_id = event.id

    try:
        content = validate_content(generator.generate(captured_snapshot, surfaces), surfaces)
    except HTTPException:
        raise
    except Exception:
        LOGGER.warning("campaign_generation_failed event_id=%s", captured_event_id)
        raise HTTPException(503, "CAMPAIGN_GENERATION_FAILED") from None

    with sessions.begin() as db:
        event = _find_event(db, captured_event_id, lock=True)
        _check_readiness(event)
        _check_generation_inputs(event, captured_snapshot)
        campaign = _find_primary(db, event.id)
        if campaign and campaign.current_version_id:
            return campaign_view(db, campaign, event)
        if campaign is None:
            at = datetime.now(UTC)
            campaign = Campaign(
                id=uuid.uuid4(),
                event_id=event.id,
                state="DRAFT",
                revision=1,
                current_version_id=None,
                created_by=actor_id,
                created_at=at,
                updated_at=at,
            )
            db.add(campaign)
            db.flush()
        _create_version(
            db,
            campaign,
            captured_snapshot,
            content,
            surfaces,
            actor_id,
            revision_request=None,
            revision_targets=[],
        )
        db.flush()
        return campaign_view(db, campaign, event)


def revise_campaign(
    sessions: sessionmaker,
    campaign_id,
    actor_id,
    expected_revision: int,
    revision_request: str,
    revision_service: CampaignRevisionService,
) -> dict:
    cleaned = revision_request.strip()
    if not 3 <= len(cleaned) <= 500:
        raise HTTPException(422, "REVISION_REQUEST_INVALID")
    if is_event_fact_revision(cleaned):
        raise HTTPException(409, "EVENT_CORRECTION_REQUIRED")
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        if campaign.revision != expected_revision:
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        _check_readiness(event)
        if not campaign.current_version_id:
            raise HTTPException(409, "CAMPAIGN_VERSION_NOT_FOUND")
        current = db.get(CampaignVersion, campaign.current_version_id)
        if is_stale(event, current):
            raise HTTPException(409, "CAMPAIGN_STALE_REGENERATE_FIRST")
        content = CampaignContent.model_validate(current.snapshot["content"], strict=True)
        selected = validate_surfaces(current.snapshot["selected_surfaces"])
        captured_snapshot = material_event_snapshot(event)
        captured_version_id = current.id
    try:
        revised, targets = revision_service.revise(
            content,
            captured_snapshot,
            cleaned,
            selected,
        )
        revised = validate_content(revised, selected)
    except HTTPException:
        raise
    except Exception:
        LOGGER.warning("campaign_revision_failed campaign_id=%s", campaign_id)
        raise HTTPException(503, "CAMPAIGN_REVISION_FAILED") from None

    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        if campaign.revision != expected_revision:
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        _check_readiness(event)
        _check_generation_inputs(event, captured_snapshot)
        if campaign.current_version_id != captured_version_id:
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        from app.execution_service import prepare_for_successor

        prepare_for_successor(db, campaign, event, actor_id)
        _create_version(
            db,
            campaign,
            captured_snapshot,
            revised,
            selected,
            actor_id,
            revision_request=cleaned,
            revision_targets=targets,
        )
        db.flush()
        return campaign_view(db, campaign, event)


def regenerate_campaign(
    sessions: sessionmaker,
    campaign_id,
    actor_id,
    expected_revision: int,
    generator: CampaignGenerator,
) -> dict:
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        if campaign.revision != expected_revision:
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        _check_readiness(event)
        if not campaign.current_version_id:
            raise HTTPException(409, "CAMPAIGN_VERSION_NOT_FOUND")
        previous = db.get(CampaignVersion, campaign.current_version_id)
        if not is_stale(event, previous):
            raise HTTPException(409, "CAMPAIGN_NOT_STALE")
        surfaces = validate_surfaces(previous.snapshot["selected_surfaces"])
        captured_snapshot = material_event_snapshot(event)
        captured_version_id = previous.id
    try:
        content = validate_content(generator.generate(captured_snapshot, surfaces), surfaces)
    except HTTPException:
        raise
    except Exception:
        LOGGER.warning("campaign_regeneration_failed campaign_id=%s", campaign_id)
        raise HTTPException(503, "CAMPAIGN_GENERATION_FAILED") from None

    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        if (
            campaign.revision != expected_revision
            or campaign.current_version_id != captured_version_id
        ):
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        _check_readiness(event)
        _check_generation_inputs(event, captured_snapshot)
        previous = db.get(CampaignVersion, campaign.current_version_id)
        if not is_stale(event, previous):
            raise HTTPException(409, "CAMPAIGN_NOT_STALE")
        from app.execution_service import prepare_for_successor

        prepare_for_successor(db, campaign, event, actor_id)
        _create_version(
            db,
            campaign,
            captured_snapshot,
            content,
            surfaces,
            actor_id,
            revision_request="Regenerated after Event details changed.",
            revision_targets=["all selected content surfaces"],
            inherit_primary_graphic=False,
        )
        db.flush()
        return campaign_view(db, campaign, event)
