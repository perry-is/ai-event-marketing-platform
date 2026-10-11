"""Exact-version approval and deterministic simulated execution orchestration."""

import copy
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.campaign_service import (
    _version_graphic,
    business_event_projection,
    campaign_view,
    canonical_json,
    is_stale,
    material_event_snapshot,
    snapshot_hash,
)
from app.event_service import event_readiness
from app.eventbrite import ADAPTER_REVISION as EVENTBRITE_ADAPTER_REVISION
from app.eventbrite import (
    EVENT_URL,
    EventbriteClient,
    EventbriteLiveAdapter,
    EventbriteRepository,
    PrivateTokenCredentials,
    account_key,
    listing_facts,
)
from app.group_recommendations import assert_targets_available, selection_pending
from app.instagram import (
    ADAPTER_REVISION as INSTAGRAM_ADAPTER_REVISION,
)
from app.instagram import (
    BLOCK_MESSAGES as INSTAGRAM_BLOCK_MESSAGES,
)
from app.instagram import (
    InstagramAPIClient,
    InstagramLiveAdapter,
    InstagramRepository,
    UnavailableInstagramCredentials,
    VaultInstagramCredentials,
)
from app.linkedin import (
    BLOCK_MESSAGES as LINKEDIN_BLOCK_MESSAGES,
)
from app.linkedin import (
    LinkedInLiveAdapter,
    LinkedInRepository,
    LinkedInRestClient,
    UnavailableLinkedInCredentials,
    VaultLinkedInCredentials,
)
from app.meetup import (
    ADAPTER_REVISION as MEETUP_ADAPTER_REVISION,
)
from app.meetup import (
    MEETUP_EVENT_URL,
    MeetupGraphQLClient,
    MeetupJWTTokenProvider,
    MeetupLiveAdapter,
    MeetupRepository,
)
from app.models import (
    Approval,
    AuditEvent,
    Campaign,
    CampaignVersion,
    DestinationExecution,
    Event,
    ExecutionAttempt,
)
from app.simulated_connectors import (
    AUTO_DESTINATIONS,
    DestinationAdapterRegistry,
    FacebookGroupsManualAdapter,
    NormalizedResult,
    SimulationScenarioController,
)

EXECUTION_LEASE_DURATION = timedelta(minutes=30)

AUTO_CONTENT_FIELD = {
    "EVENTBRITE": "eventbrite_description",
    "MEETUP": "meetup_description",
    "LINKEDIN_ORGANIZATION": "linkedin_organization_post",
    "INSTAGRAM": "instagram_caption",
}
MANUAL_CONTENT_FIELD = {"FACEBOOK_GROUPS": "facebook_groups_post"}
UNVERIFIED_CONTENT_ONLY = {
    "LINKEDIN_PERSONAL": (
        "Event Marketing identity and routing are UNVERIFIED; this copy is not executable in WP1E."
    )
}
SIMULATED_TARGET = "simulated-default"
MANUAL_GROUP_TARGET = "manual-group-selection-deferred"


def _reauthorization_callback(credentials):
    callback = getattr(credentials, "mark_reauthorization_required", None)
    return callback if callable(callback) else None


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(value: dict) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_TRANSIENT_PLAN_KEYS = {
    "campaign_version_id",
    "created_at",
    "updated_at",
    "readiness",
    "readiness_message",
    "connector_health",
    "request_id",
    "ui_label",
    "preview_url",
}

# These destination-specific restrictions are deterministic, version-visible limits,
# not missing credentials or transient provider failures. Approval may proceed so the
# destination can be explicitly accounted for alongside the other approved work.
_ACCOUNTABLE_APPROVAL_BLOCKERS = {
    "INSTAGRAM_ALREADY_PUBLISHED_IMMUTABLE",
    "LINKEDIN_MEDIA_REPLACEMENT_UNSUPPORTED",
}


def _clean_plan_value(value):
    if isinstance(value, dict):
        return {
            key: _clean_plan_value(child)
            for key, child in value.items()
            if key not in _TRANSIENT_PLAN_KEYS
        }
    if isinstance(value, list):
        return [_clean_plan_value(child) for child in value]
    return value


def destination_plan_projection(plan: dict) -> dict:
    """Stable business-significant authorization projection, including legacy plans."""
    if not isinstance(plan, dict):
        raise HTTPException(409, "APPROVED_PLAN_INVALID")
    normalized = {
        key: _clean_plan_value(value)
        for key, value in plan.items()
        if key not in _TRANSIENT_PLAN_KEYS | {"runtime_mode"}
    }
    targets = []
    for source in plan.get("targets", []):
        entry = _clean_plan_value(source)
        entry.pop("runtime_mode", None)
        if entry.get("execution_kind") == "AUTO":
            mode = source.get("runtime_mode", plan.get("runtime_mode"))
            if mode not in {"SIMULATED", "LIVE"}:
                raise HTTPException(409, "APPROVED_PLAN_INVALID")
            entry["runtime_mode"] = mode
        elif entry.get("execution_kind") != "MANUAL":
            raise HTTPException(409, "APPROVED_PLAN_INVALID")
        groups = entry.get("selected_groups")
        if isinstance(groups, list):
            entry["selected_groups"] = sorted(
                groups,
                key=lambda group: (
                    str(group.get("group_record_id", "")),
                    str(group.get("url", "")),
                ),
            )
        targets.append(entry)
    normalized["targets"] = sorted(
        targets,
        key=lambda entry: (
            str(entry.get("destination_key", "")),
            str(entry.get("target_key", "")),
        ),
    )
    return normalized


def destination_plan_hash(plan: dict) -> str:
    return _hash(destination_plan_projection(plan))


def _plan_core_hash(plan: dict) -> str:
    projection = destination_plan_projection(plan)
    for entry in projection["targets"]:
        entry.pop("replacement_ack_execution_ids", None)
        entry.pop("uncertain_risk_ack_execution_ids", None)
    return _hash(projection)


def _event_snapshot(version: CampaignVersion) -> dict:
    return version.snapshot["event"]


def _plan_entry(
    version: CampaignVersion,
    destination: str,
    field_name: str,
    primary_graphic: dict | None,
    runtime_modes: dict[str, str],
    live_accounts: dict[str, dict] | None = None,
    live_targets: dict[str, dict] | None = None,
) -> dict:
    content = version.snapshot["content"]
    body = content.get(field_name)
    if not isinstance(body, str) or not body.strip():
        raise HTTPException(409, "DESTINATION_CONTENT_MISSING")
    entry = {
        "destination_key": destination,
        "target_key": (
            SIMULATED_TARGET if destination in AUTO_DESTINATIONS else MANUAL_GROUP_TARGET
        ),
        "execution_kind": "AUTO" if destination in AUTO_DESTINATIONS else "MANUAL",
        "campaign_version_id": str(version.id),
        "event_title": _event_snapshot(version).get("title") or "Event Marketing event",
        "approved_content": body,
        "content_field": field_name,
        "creative_direction": content.get("creative_direction"),
        "group_selection_status": None,
    }
    entry["ui_label"] = {
        "EVENTBRITE": "Event Marketing Organization",
        "MEETUP": "Event Marketing Meetup Group",
        "LINKEDIN_ORGANIZATION": "Event Marketing Page",
        "INSTAGRAM": "Event Marketing Instagram",
    }.get(destination, destination.replace("_", " ").title())
    if destination in AUTO_DESTINATIONS:
        entry["runtime_mode"] = runtime_modes[destination]
        account = (live_accounts or {}).get(destination)
        if entry["runtime_mode"] == "LIVE" and destination == "EVENTBRITE" and account:
            # The approved account and exact listing facts are frozen into the plan hash.
            entry["target_key"] = f"eventbrite-{account_key(account)}"
            entry["eventbrite"] = {
                "account": dict(account),
                "listing": listing_facts(_event_snapshot(version)),
            }
        if entry["runtime_mode"] == "LIVE" and destination == "MEETUP":
            target = dict((live_targets or {}).get(destination) or {})
            group_name = target.get("group_urlname") or ""
            target["ui_label"] = " ".join(group_name.replace("-", " ").split()).title()
            if not target["ui_label"]:
                target["ui_label"] = "Event Marketing Meetup Group"
            entry["meetup"] = target
            entry["ui_label"] = target["ui_label"]
            fingerprint = target.get("account_fingerprint") or "unconfigured"
            group_urlname = target.get("group_urlname") or "unconfigured"
            entry["target_key"] = f"meetup:{group_urlname}:{fingerprint[:16]}"
            entry["meetup_event"] = listing_facts(_event_snapshot(version))
        if entry["runtime_mode"] == "LIVE" and destination == "LINKEDIN_ORGANIZATION":
            target = dict((live_targets or {}).get(destination) or {})
            entry["linkedin_organization"] = target
            entry["ui_label"] = target.get("ui_label") or "Event Marketing Page"
            organization_urn = target.get("organization_urn") or "unconfigured"
            entry["target_key"] = f"linkedin-organization:{organization_urn}"
        if entry["runtime_mode"] == "LIVE" and destination == "INSTAGRAM":
            target = dict((live_targets or {}).get(destination) or {})
            entry["instagram_account"] = target
            entry["ui_label"] = target.get("ui_label") or "Event Marketing Instagram"
            account_id = target.get("account_id") or "unconfigured"
            entry["target_key"] = f"instagram-professional-account:{account_id}"
    if destination == "FACEBOOK_GROUPS":
        groups = version.snapshot.get("facebook_group_targets", [])
        entry["group_selection_status"] = (
            "GROUPS_SELECTED" if groups else "DEFERRED_NO_GROUPS_SELECTED"
        )
        entry["selected_groups"] = [
            {
                "group_record_id": item["group_record_id"],
                "display_name": item["display_name"],
                "url": item["chosen_url"],
                "geography_label": item.get("geography_label"),
                "member_count": item.get("member_count"),
                "cautions": list(item.get("cautions", [])),
            }
            for item in groups
        ]
        entry["ui_label"] = f"{len(entry['selected_groups'])} selected Groups"
    if primary_graphic is not None:
        entry["approved_graphic"] = {
            "asset_id": primary_graphic["asset_id"],
            "sha256": primary_graphic["sha256"],
            "mime_type": primary_graphic["mime_type"],
            "width": primary_graphic["width"],
            "height": primary_graphic["height"],
            "preview_url": primary_graphic["preview_url"],
        }
    return entry


def build_destination_plan(
    version: CampaignVersion,
    primary_graphic: dict | None = None,
    runtime_modes: dict[str, str] | None = None,
    live_accounts: dict[str, dict] | None = None,
    live_targets: dict[str, dict] | None = None,
) -> dict:
    runtime_modes = runtime_modes or dict.fromkeys(AUTO_DESTINATIONS, "SIMULATED")
    selected = version.snapshot.get("selected_surfaces")
    if not isinstance(selected, list) or not selected:
        raise HTTPException(409, "CAMPAIGN_DESTINATION_PLAN_INVALID")
    targets = []
    exclusions = []
    content_only = []
    for surface in selected:
        if surface in UNVERIFIED_CONTENT_ONLY:
            field_name = "linkedin_personal_post"
            content = version.snapshot.get("content", {}).get(field_name)
            if content:
                content_only.append(
                    {
                        "destination_key": surface,
                        "content_field": field_name,
                        "reason": UNVERIFIED_CONTENT_ONLY[surface],
                    }
                )
            exclusions.append(
                {"destination_key": surface, "reason": "EVENT_MARKETING_ACCESS_UNVERIFIED"}
            )
        elif surface in AUTO_CONTENT_FIELD:
            targets.append(
                _plan_entry(
                    version,
                    surface,
                    AUTO_CONTENT_FIELD[surface],
                    primary_graphic,
                    runtime_modes,
                    live_accounts,
                    live_targets,
                )
            )
        elif surface in MANUAL_CONTENT_FIELD:
            targets.append(
                _plan_entry(
                    version,
                    surface,
                    MANUAL_CONTENT_FIELD[surface],
                    primary_graphic,
                    runtime_modes,
                    None,
                    live_targets,
                )
            )
        else:
            raise HTTPException(409, "CAMPAIGN_DESTINATION_UNSUPPORTED")
    if not targets:
        raise HTTPException(409, "CAMPAIGN_DESTINATION_PLAN_EMPTY")
    return {
        "schema_version": 2,
        "targets": targets,
        "exclusions": exclusions,
        "content_only": content_only,
    }


def _find_campaign(db: Session, campaign_id, *, lock: bool = False) -> tuple[Campaign, Event]:
    try:
        parsed = uuid.UUID(str(campaign_id))
    except ValueError:
        raise HTTPException(404, "CAMPAIGN_NOT_FOUND") from None
    campaign = db.scalar(select(Campaign).where(Campaign.id == parsed))
    if campaign is None:
        raise HTTPException(404, "CAMPAIGN_NOT_FOUND")
    event_query = select(Event).where(Event.id == campaign.event_id)
    if lock:
        event_query = event_query.with_for_update()
    event = db.scalar(event_query)
    if event is None:
        raise HTTPException(404, "EVENT_NOT_FOUND")
    if lock:
        campaign = db.scalar(select(Campaign).where(Campaign.id == parsed).with_for_update())
    return campaign, event


def _find_version(db: Session, campaign: Campaign, version_id) -> CampaignVersion:
    try:
        parsed = uuid.UUID(str(version_id))
    except ValueError:
        raise HTTPException(404, "CAMPAIGN_VERSION_NOT_FOUND") from None
    version = db.scalar(
        select(CampaignVersion).where(
            CampaignVersion.id == parsed,
            CampaignVersion.campaign_id == campaign.id,
        )
    )
    if version is None:
        raise HTTPException(404, "CAMPAIGN_VERSION_NOT_FOUND")
    return version


def _verify_version_integrity(version: CampaignVersion) -> None:
    if (
        canonical_json(version.snapshot) != version.canonical_snapshot_text
        or snapshot_hash(version.snapshot) != version.content_hash
    ):
        raise HTTPException(409, "CAMPAIGN_VERSION_HASH_INVALID")


def _prior_effect_requirements(
    db: Session,
    campaign: Campaign,
    plan: dict,
    *,
    exclude_approval_id=None,
) -> tuple[list[dict], list[dict], list[dict]]:
    replacements: list[dict] = []
    uncertain: list[dict] = []
    blockers: list[dict] = []
    for target in plan["targets"]:
        rows = db.execute(
            select(DestinationExecution, CampaignVersion.version_number)
            .join(CampaignVersion, CampaignVersion.id == DestinationExecution.campaign_version_id)
            .where(
                DestinationExecution.campaign_id == campaign.id,
                DestinationExecution.destination_key == target["destination_key"],
                DestinationExecution.target_key == target["target_key"],
            )
            .order_by(CampaignVersion.version_number)
        ).all()
        for execution, version_number in rows:
            if execution.approval_id == exclude_approval_id:
                continue
            item = {
                "execution_id": str(execution.id),
                "destination_key": execution.destination_key,
                "version_number": version_number,
                "status": execution.status,
                "runtime_mode": execution.runtime_mode,
            }
            if execution.status in {"UNKNOWN", "RUNNING"}:
                if execution.disposition.get("type") == "ABANDONED_UNCERTAIN":
                    uncertain.append(item)
                else:
                    blockers.append(item)
            elif (
                execution.status in {"SUCCEEDED", "MANUAL_DONE"}
                or (execution.status == "MANUAL_PENDING")
                or (execution.status == "SKIPPED" and execution.manual_package is not None)
            ):
                replacements.append(item)
    return replacements, uncertain, blockers


def _attach_effect_acknowledgements(plan: dict, replacements: list[dict], uncertain: list[dict]):
    for target in plan["targets"]:
        target["replacement_ack_execution_ids"] = sorted(
            item["execution_id"]
            for item in replacements
            if item["destination_key"] == target["destination_key"]
        )
        target["uncertain_risk_ack_execution_ids"] = sorted(
            item["execution_id"]
            for item in uncertain
            if item["destination_key"] == target["destination_key"]
        )


def _destination_readiness(plan: dict, adapters: DestinationAdapterRegistry) -> list[dict]:
    result = []
    for target in plan["targets"]:
        status, reason = adapters.readiness(target)
        result.append(
            {
                "destination_key": target["destination_key"],
                "target_key": target["target_key"],
                "execution_kind": target["execution_kind"],
                "runtime_mode": target.get("runtime_mode"),
                "status": status,
                "reason": reason,
            }
        )
    return result


def _can_approve_readiness(readiness: list[dict]) -> bool:
    return all(
        item["status"] == "READY"
        or (item["status"] == "BLOCKED" and item.get("reason") in _ACCOUNTABLE_APPROVAL_BLOCKERS)
        for item in readiness
    )


def _readiness_human_message(reason: str | None) -> str:
    if reason:
        for messages in (INSTAGRAM_BLOCK_MESSAGES, LINKEDIN_BLOCK_MESSAGES):
            if reason in messages:
                return messages[reason]
    return "The approved destination mode is unavailable. No alternate mode was used."


def approval_preview(
    sessions: sessionmaker, campaign_id, adapters: DestinationAdapterRegistry
) -> dict:
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        if campaign.current_version_id is None:
            raise HTTPException(409, "CAMPAIGN_VERSION_NOT_FOUND")
        version = db.get(CampaignVersion, campaign.current_version_id)
        _verify_version_integrity(version)
        plan = build_destination_plan(
            version,
            _version_graphic(db, version),
            adapters.runtime_modes,
            adapters.live_accounts,
            adapters.live_targets,
        )
        active = (
            db.get(Approval, campaign.active_approval_id) if campaign.active_approval_id else None
        )
        exclude_approval_id = (
            active.id
            if active
            and active.campaign_version_id == version.id
            and _plan_core_hash(active.destination_plan) == _plan_core_hash(plan)
            else None
        )
        replacements, uncertain, blockers = _prior_effect_requirements(
            db, campaign, plan, exclude_approval_id=exclude_approval_id
        )
        _attach_effect_acknowledgements(plan, replacements, uncertain)
        destination_readiness = _destination_readiness(plan, adapters)
        proposed_plan_hash = destination_plan_hash(plan)
        changed_active_plan = bool(
            active
            and active.campaign_version_id == version.id
            and active.version_hash == version.content_hash
            and active.destination_plan_hash != proposed_plan_hash
        )
        groups_pending = selection_pending(db, campaign, version)
        return {
            "campaign": campaign_view(db, campaign, event),
            "version_id": str(version.id),
            "version_number": version.version_number,
            "version_hash": version.content_hash,
            "destination_plan": plan,
            "destination_plan_hash": proposed_plan_hash,
            "destination_readiness": destination_readiness,
            "replacement_requirements": replacements,
            "uncertain_risk_requirements": uncertain,
            "blocking_unknowns": blockers,
            "group_selection_pending": groups_pending,
            "can_approve": bool(
                (campaign.state == "REVIEW" or changed_active_plan)
                and not is_stale(event, version)
                and event_readiness(event)["ready"]
                and not blockers
                and not groups_pending
                and _can_approve_readiness(destination_readiness)
            ),
        }


def _audit(
    db: Session,
    action: str,
    *,
    actor_id=None,
    event_id=None,
    campaign_id=None,
    campaign_version_id=None,
    approval_id=None,
    execution_id=None,
    attempt_id=None,
    reason: str | None = None,
    metadata: dict | None = None,
    actor_type: str = "USER",
    correlation_id=None,
) -> AuditEvent:
    row = AuditEvent(
        id=uuid.uuid4(),
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        event_id=event_id,
        campaign_id=campaign_id,
        campaign_version_id=campaign_version_id,
        approval_id=approval_id,
        destination_execution_id=execution_id,
        attempt_id=attempt_id,
        correlation_id=correlation_id or uuid.uuid4(),
        reason=reason,
        safe_metadata=metadata or {},
        occurred_at=_now(),
    )
    db.add(row)
    return row


def approve_version(
    sessions: sessionmaker,
    campaign_id,
    actor_id,
    adapters: DestinationAdapterRegistry,
    *,
    expected_revision: int,
    version_id,
    version_hash: str,
    expected_destination_plan_hash: str,
    replacement_acknowledgements: list[str],
    uncertain_risk_acknowledgements: list[str],
) -> dict:
    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        if campaign.revision != expected_revision:
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        version = _find_version(db, campaign, version_id)
        plan = build_destination_plan(
            version,
            _version_graphic(db, version),
            adapters.runtime_modes,
            adapters.live_accounts,
            adapters.live_targets,
        )
        active = (
            db.get(Approval, campaign.active_approval_id) if campaign.active_approval_id else None
        )
        exclude_approval_id = (
            active.id
            if active
            and active.campaign_version_id == version.id
            and _plan_core_hash(active.destination_plan) == _plan_core_hash(plan)
            else None
        )
        replacements, uncertain, blockers = _prior_effect_requirements(
            db, campaign, plan, exclude_approval_id=exclude_approval_id
        )
        _attach_effect_acknowledgements(plan, replacements, uncertain)
        plan_hash = destination_plan_hash(plan)
        if expected_destination_plan_hash != plan_hash:
            raise HTTPException(409, "APPROVAL_PLAN_CHANGED")
        if campaign.current_version_id != version.id:
            if (
                active
                and active.campaign_version_id == version.id
                and active.version_hash == version.content_hash
                and active.destination_plan_hash == plan_hash
            ):
                return _approval_view(active)
            raise HTTPException(409, "CAMPAIGN_VERSION_NOT_CURRENT")
        if not event_readiness(event)["ready"]:
            raise HTTPException(409, "EVENT_NOT_READY")
        if is_stale(event, version):
            raise HTTPException(409, "CAMPAIGN_STALE")
        _verify_version_integrity(version)
        if version_hash != version.content_hash:
            raise HTTPException(409, "CAMPAIGN_VERSION_HASH_INVALID")
        if selection_pending(db, campaign, version):
            raise HTTPException(409, "GROUP_SELECTION_NOT_SAVED")
        assert_targets_available(db, version.snapshot.get("facebook_group_targets", []))
        if campaign.state != "REVIEW":
            if not active or active.campaign_version_id != version.id:
                raise HTTPException(409, "CAMPAIGN_VERSION_NOT_CURRENT")
            if active.destination_plan_hash == plan_hash:
                return _approval_view(active)
        readiness = _destination_readiness(plan, adapters)
        if not _can_approve_readiness(readiness):
            unavailable = next(
                item
                for item in readiness
                if item["status"] != "READY"
                and item.get("reason") not in _ACCOUNTABLE_APPROVAL_BLOCKERS
            )
            raise HTTPException(409, unavailable["reason"] or "DESTINATION_NOT_READY")
        if blockers:
            raise HTTPException(409, "PREVIOUS_UNKNOWN_REQUIRES_RECONCILIATION")
        replacement_ids = set(replacement_acknowledgements)
        uncertain_ids = set(uncertain_risk_acknowledgements)
        required_replacements = {item["execution_id"] for item in replacements}
        required_uncertain = {item["execution_id"] for item in uncertain}
        if not required_replacements.issubset(replacement_ids):
            raise HTTPException(409, "REPLACEMENT_CONFIRMATION_REQUIRED")
        if not required_uncertain.issubset(uncertain_ids):
            raise HTTPException(409, "UNCERTAIN_RISK_ACKNOWLEDGEMENT_REQUIRED")
        if not replacement_ids.issubset(required_replacements) or not uncertain_ids.issubset(
            required_uncertain
        ):
            raise HTTPException(422, "ACKNOWLEDGEMENT_NOT_APPLICABLE")
        if (
            active
            and active.campaign_version_id == version.id
            and active.destination_plan_hash == plan_hash
        ):
            return _approval_view(active)
        approval = Approval(
            id=uuid.uuid4(),
            campaign_id=campaign.id,
            campaign_version_id=version.id,
            approver_id=actor_id,
            version_hash=version.content_hash,
            approved_plan_hash=None,
            destination_plan_hash=plan_hash,
            destination_plan=plan,
            event_revision=version.event_revision,
            runtime_mode=None,
            approved_at=_now(),
        )
        db.add(approval)
        db.flush()
        campaign.active_approval_id = approval.id
        campaign.state = "APPROVED"
        campaign.revision += 1
        campaign.updated_at = _now()
        _audit(
            db,
            "CAMPAIGN_VERSION_APPROVED",
            actor_id=actor_id,
            event_id=event.id,
            campaign_id=campaign.id,
            campaign_version_id=version.id,
            approval_id=approval.id,
            metadata={
                "destination_plan_hash": plan_hash,
                "destination_count": len(plan["targets"]),
            },
        )
        return _approval_view(approval)


def _approval_view(approval: Approval) -> dict:
    return {
        "id": str(approval.id),
        "campaign_id": str(approval.campaign_id),
        "campaign_version_id": str(approval.campaign_version_id),
        "version_hash": approval.version_hash,
        "destination_plan_hash": approval.destination_plan_hash,
        "event_revision": approval.event_revision,
        "approved_at": approval.approved_at.isoformat(),
        "destination_plan": copy.deepcopy(approval.destination_plan),
    }


def _check_approval_integrity(approval: Approval, version: CampaignVersion) -> None:
    _verify_version_integrity(version)
    if approval.campaign_version_id != version.id or approval.version_hash != version.content_hash:
        raise HTTPException(409, "APPROVAL_VERSION_MISMATCH")
    if destination_plan_hash(approval.destination_plan) != approval.destination_plan_hash:
        raise HTTPException(409, "APPROVED_PLAN_HASH_INVALID")


def _find_approval(db: Session, approval_id) -> Approval:
    try:
        parsed = uuid.UUID(str(approval_id))
    except ValueError:
        raise HTTPException(404, "APPROVAL_NOT_FOUND") from None
    approval = db.scalar(select(Approval).where(Approval.id == parsed))
    if approval is None:
        raise HTTPException(404, "APPROVAL_NOT_FOUND")
    return approval


def _assert_active_authority(
    campaign: Campaign, event: Event, approval: Approval, version: CampaignVersion
) -> None:
    if campaign.active_approval_id != approval.id:
        raise HTTPException(409, "APPROVAL_NOT_ACTIVE")
    if campaign.current_version_id != version.id:
        raise HTTPException(409, "APPROVAL_VERSION_NOT_CURRENT")
    _check_approval_integrity(approval, version)
    if is_stale(event, version):
        raise HTTPException(409, "APPROVAL_SUPERSEDED_BY_EVENT_CHANGE")


def _idempotency_key(version_id, target: dict, plan_hash: str) -> str:
    return _hash(
        {
            "campaign_version_id": str(version_id),
            "destination_key": target["destination_key"],
            "target_key": target["target_key"],
            "runtime_mode": target.get("runtime_mode"),
            "destination_plan_hash": plan_hash,
        }
    )


def _attempt_fingerprint(execution: DestinationExecution, number: int, operation: str) -> str:
    return _hash(
        {
            "execution_id": str(execution.id),
            "approval_id": str(execution.approval_id),
            "idempotency_key": execution.idempotency_key,
            "destination_plan_hash": execution.destination_plan_hash,
            "attempt_number": number,
            "operation": operation,
        }
    )


def _next_attempt_number(db: Session, execution_id) -> int:
    current = db.scalar(
        select(func.max(ExecutionAttempt.attempt_number)).where(
            ExecutionAttempt.execution_id == execution_id
        )
    )
    return (current or 0) + 1


def recover_expired_execution_attempts(sessions: sessionmaker) -> int:
    """Convert expired active ownership to uncertainty without reissuing provider work."""
    now = _now()
    with sessions() as db:
        candidates = db.execute(
            select(
                ExecutionAttempt.id,
                ExecutionAttempt.execution_id,
                DestinationExecution.campaign_id,
            )
            .join(DestinationExecution, DestinationExecution.id == ExecutionAttempt.execution_id)
            .where(
                ExecutionAttempt.status == "RUNNING",
                ExecutionAttempt.lease_expires_at.is_not(None),
                ExecutionAttempt.lease_expires_at <= now,
                (
                    (ExecutionAttempt.operation == "PUBLISH")
                    & (DestinationExecution.status == "RUNNING")
                    | (ExecutionAttempt.operation == "RECONCILE")
                    & (DestinationExecution.status == "UNKNOWN")
                ),
            )
        ).all()

    recovered = 0
    for attempt_id, execution_id, campaign_id in candidates:
        with sessions.begin() as db:
            campaign, event = _find_campaign(db, campaign_id, lock=True)
            execution = db.scalar(
                select(DestinationExecution)
                .where(DestinationExecution.id == execution_id)
                .with_for_update()
            )
            attempt = db.scalar(
                select(ExecutionAttempt).where(ExecutionAttempt.id == attempt_id).with_for_update()
            )
            if (
                attempt is None
                or attempt.status != "RUNNING"
                or attempt.lease_expires_at is None
                or attempt.lease_expires_at > now
            ):
                continue
            if attempt.operation == "PUBLISH" and execution.status == "RUNNING":
                message = (
                    "The destination result was interrupted and may have been applied. "
                    "Verify it before retrying."
                )
                attempt.status = "FINALIZED"
                attempt.outcome = "UNCERTAIN"
                attempt.finished_at = now
                attempt.error_category = "AMBIGUOUS_OUTCOME"
                attempt.human_error = message
                attempt.retryable = False
                attempt.verification_status = "INCONCLUSIVE"
                attempt.lease_expires_at = None
                attempt.evidence = {
                    **(attempt.evidence or {}),
                    "recovery": "EXECUTION_INTERRUPTED",
                    "recovered_at": now.isoformat(),
                }
                execution.status = "UNKNOWN"
                execution.result_summary = message
                execution.error_category = "AMBIGUOUS_OUTCOME"
                execution.human_error = message
                execution.retryable = False
                execution.verification_status = "INCONCLUSIVE"
                execution.disposition = {}
                execution.revision += 1
                execution.updated_at = now
                action = "DESTINATION_EXECUTION_RECOVERED_UNKNOWN"
                metadata = {
                    "destination_key": execution.destination_key,
                    "attempt_number": attempt.attempt_number,
                    "reason": "EXECUTION_INTERRUPTED",
                }
            elif attempt.operation == "RECONCILE" and execution.status == "UNKNOWN":
                message = "Verification was interrupted; the destination result remains uncertain."
                attempt.status = "FINALIZED"
                attempt.outcome = "VERIFICATION_RESULT"
                attempt.finished_at = now
                attempt.error_category = "AMBIGUOUS_OUTCOME"
                attempt.human_error = message
                attempt.retryable = False
                attempt.verification_status = "INCONCLUSIVE"
                attempt.lease_expires_at = None
                attempt.evidence = {
                    **(attempt.evidence or {}),
                    "recovery": "RECONCILIATION_INTERRUPTED",
                    "recovered_at": now.isoformat(),
                }
                execution.revision += 1
                execution.updated_at = now
                action = "RECONCILIATION_RECOVERED_INCONCLUSIVE"
                metadata = {
                    "destination_key": execution.destination_key,
                    "attempt_number": attempt.attempt_number,
                    "reason": "RECONCILIATION_INTERRUPTED",
                }
            else:
                continue
            approval = db.get(Approval, execution.approval_id)
            _audit(
                db,
                action,
                actor_type="SYSTEM",
                event_id=event.id,
                campaign_id=campaign.id,
                campaign_version_id=execution.campaign_version_id,
                approval_id=approval.id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                reason=metadata["reason"],
                metadata=metadata,
            )
            if campaign.active_approval_id == approval.id:
                _refresh_campaign_state(db, campaign, approval)
            recovered += 1
    return recovered


def _set_campaign_state(campaign: Campaign, state: str) -> None:
    if campaign.state != state:
        campaign.state = state
        campaign.revision += 1
    campaign.updated_at = _now()


def _execution_complete(execution: DestinationExecution) -> bool:
    status = execution.status
    disposition_type = execution.disposition.get("type")
    if status in {"SUCCEEDED", "MANUAL_DONE", "SKIPPED"}:
        return True
    if status in {"FAILED", "BLOCKED"}:
        return disposition_type == "ACKNOWLEDGED_FAILURE"
    if status == "UNKNOWN":
        return disposition_type == "ABANDONED_UNCERTAIN"
    if status == "MANUAL_PENDING":
        return disposition_type == "MANUAL_HANDOFF"
    return False


def _campaign_outcome(executions: list[DestinationExecution], expected: int) -> tuple[bool, str]:
    if not executions or len(executions) < expected:
        return False, "APPROVED_NOT_STARTED"
    if any(value.status == "UNKNOWN" and not _execution_complete(value) for value in executions):
        return False, "UNRESOLVED_UNKNOWN"
    if not all(_execution_complete(value) for value in executions):
        return False, "ACTION_REQUIRED"
    statuses = {value.status for value in executions}
    dispositions = {value.disposition.get("type") for value in executions}
    if "ABANDONED_UNCERTAIN" in dispositions:
        return True, "COMPLETED_WITH_ACKNOWLEDGED_UNCERTAINTY"
    if "ACKNOWLEDGED_FAILURE" in dispositions or "SKIPPED" in statuses:
        return True, "COMPLETED_WITH_ACKNOWLEDGED_FAILURES"
    if "MANUAL_DONE" in statuses or "MANUAL_HANDOFF" in dispositions:
        return True, "COMPLETED_WITH_MANUAL_ACTIONS"
    return True, "COMPLETED_SUCCESSFULLY"


def _refresh_campaign_state(db: Session, campaign: Campaign, approval: Approval) -> str:
    rows = db.scalars(
        select(DestinationExecution).where(DestinationExecution.approval_id == approval.id)
    ).all()
    expected = len(approval.destination_plan["targets"])
    complete, outcome = _campaign_outcome(rows, expected)
    if complete:
        was_complete = campaign.state == "COMPLETE"
        _set_campaign_state(campaign, "COMPLETE")
        if not was_complete:
            _audit(
                db,
                "CAMPAIGN_EXECUTION_COMPLETED",
                actor_type="SYSTEM",
                campaign_id=campaign.id,
                campaign_version_id=approval.campaign_version_id,
                approval_id=approval.id,
                metadata={"outcome": outcome, "target_count": expected},
            )
    elif rows:
        _set_campaign_state(campaign, "EXECUTING")
    else:
        _set_campaign_state(campaign, "APPROVED")
    return outcome


def _execution_view(db: Session, execution: DestinationExecution) -> dict:
    attempts = db.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.execution_id == execution.id)
        .order_by(ExecutionAttempt.attempt_number)
    ).all()
    return {
        "id": str(execution.id),
        "campaign_id": str(execution.campaign_id),
        "campaign_version_id": str(execution.campaign_version_id),
        "approval_id": str(execution.approval_id),
        "destination_key": execution.destination_key,
        "target_key": execution.target_key,
        "execution_kind": execution.execution_kind,
        "runtime_mode": execution.runtime_mode,
        "destination_plan_hash": execution.destination_plan_hash,
        "status": execution.status,
        "revision": execution.revision,
        "idempotency_key": execution.idempotency_key,
        "plan_entry": copy.deepcopy(execution.plan_entry),
        "manual_package": copy.deepcopy(execution.manual_package),
        "manual_package_hash": execution.manual_package_hash,
        "external_id": execution.external_id,
        "external_url": execution.external_url,
        "result_summary": execution.result_summary,
        "error_category": execution.error_category,
        "human_error": execution.human_error,
        "retryable": execution.retryable,
        "verification_status": execution.verification_status,
        "disposition": copy.deepcopy(execution.disposition),
        "created_at": execution.created_at.isoformat(),
        "updated_at": execution.updated_at.isoformat(),
        "attempts": [
            {
                "id": str(attempt.id),
                "attempt_number": attempt.attempt_number,
                "operation": attempt.operation,
                "status": attempt.status,
                "outcome": attempt.outcome,
                "started_at": attempt.started_at.isoformat(),
                "dispatched_at": (
                    attempt.dispatched_at.isoformat() if attempt.dispatched_at else None
                ),
                "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
                "external_id": attempt.external_id,
                "external_url": attempt.external_url,
                "error_category": attempt.error_category,
                "human_error": attempt.human_error,
                "retryable": attempt.retryable,
                "verification_status": attempt.verification_status,
                "evidence": copy.deepcopy(attempt.evidence),
            }
            for attempt in attempts
        ],
    }


def _approval_list_view(db: Session, campaign: Campaign) -> list[dict]:
    rows = db.scalars(
        select(Approval).where(Approval.campaign_id == campaign.id).order_by(Approval.approved_at)
    ).all()
    result = []
    for approval in rows:
        version = db.get(CampaignVersion, approval.campaign_version_id)
        approver = approval.approver_id
        result.append(
            {
                "id": str(approval.id),
                "campaign_version_id": str(approval.campaign_version_id),
                "version_number": version.version_number if version else None,
                "version_hash": approval.version_hash,
                "destination_plan_hash": approval.destination_plan_hash,
                "event_revision": approval.event_revision,
                "approved_at": approval.approved_at.isoformat(),
                "approver_id": str(approver),
                "active": campaign.active_approval_id == approval.id,
                "destination_plan": copy.deepcopy(approval.destination_plan),
            }
        )
    return result


def distribution_view(sessions: sessionmaker, campaign_id, *, recover: bool = True) -> dict:
    if recover:
        recover_expired_execution_attempts(sessions)
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        approvals = _approval_list_view(db, campaign)
        executions = db.scalars(
            select(DestinationExecution)
            .where(DestinationExecution.campaign_id == campaign.id)
            .join(CampaignVersion, CampaignVersion.id == DestinationExecution.campaign_version_id)
            .order_by(CampaignVersion.version_number, DestinationExecution.created_at)
        ).all()
        current_approval = (
            db.get(Approval, campaign.active_approval_id) if campaign.active_approval_id else None
        )
        plan = (
            current_approval.destination_plan
            if current_approval
            else (approvals[-1]["destination_plan"] if approvals else None)
        )
        current_rows = (
            db.scalars(
                select(DestinationExecution).where(
                    DestinationExecution.approval_id == current_approval.id
                )
            ).all()
            if current_approval
            else []
        )
        expected = len(plan["targets"]) if plan else 0
        complete, outcome = (
            _campaign_outcome(current_rows, expected)
            if current_approval
            else (
                False,
                "NEEDS_APPROVAL",
            )
        )
        completion_time = (
            db.scalar(
                select(AuditEvent.occurred_at)
                .where(
                    AuditEvent.action == "CAMPAIGN_EXECUTION_COMPLETED",
                    AuditEvent.approval_id == current_approval.id,
                )
                .order_by(AuditEvent.occurred_at.desc())
                .limit(1)
            )
            if current_approval and complete
            else None
        )
        return {
            "campaign": campaign_view(db, campaign, event),
            "active_approval": _approval_view(current_approval) if current_approval else None,
            "approvals": approvals,
            "executions": [_execution_view(db, value) for value in executions],
            "summary": {
                "state": campaign.state,
                "outcome": outcome,
                "complete": complete,
                "completed_at": completion_time.isoformat() if completion_time else None,
                "target_count": expected,
                "succeeded_count": sum(value.status == "SUCCEEDED" for value in current_rows),
                "failed_count": sum(value.status == "FAILED" for value in current_rows),
                "accounted_count": sum(
                    value.status in {"FAILED", "BLOCKED"}
                    and value.disposition.get("type") == "ACKNOWLEDGED_FAILURE"
                    for value in current_rows
                ),
                "unknown_count": sum(value.status == "UNKNOWN" for value in current_rows),
                "manual_pending_count": sum(
                    value.status == "MANUAL_PENDING"
                    and value.disposition.get("type") != "MANUAL_HANDOFF"
                    for value in current_rows
                ),
                "manual_done_count": sum(value.status == "MANUAL_DONE" for value in current_rows),
            },
        }


def _manual_package(plan_entry: dict) -> dict:
    return FacebookGroupsManualAdapter().produce_manual_package(plan_entry)


def initialize_execution(
    sessions: sessionmaker, approval_id, actor_id, adapters: DestinationAdapterRegistry
) -> list[str]:
    recover_expired_execution_attempts(sessions)
    with sessions() as db:
        approval = _find_approval(db, approval_id)
        campaign_id = approval.campaign_id
        version_id = approval.campaign_version_id
    ready_auto: list[str] = []
    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        approval = _find_approval(db, approval_id)
        version = _find_version(db, campaign, version_id)
        _assert_active_authority(campaign, event, approval, version)
        if not event_readiness(event)["ready"]:
            raise HTTPException(409, "EVENT_NOT_READY")
        for entry in approval.destination_plan["targets"]:
            if entry.get("selected_groups"):
                assert_targets_available(
                    db,
                    [
                        {"group_record_id": g["group_record_id"], "chosen_url": g["url"]}
                        for g in entry["selected_groups"]
                    ],
                )
        now = _now()
        created_any = False
        manual_delivered = False
        for entry in approval.destination_plan["targets"]:
            adapter = adapters.for_entry(entry)
            readiness, readiness_reason = adapters.readiness(entry)
            key = _idempotency_key(version.id, entry, approval.destination_plan_hash)
            existing = db.scalar(
                select(DestinationExecution).where(
                    DestinationExecution.approval_id == approval.id,
                    DestinationExecution.destination_key == entry["destination_key"],
                    DestinationExecution.target_key == entry["target_key"],
                )
            )
            if existing:
                if (
                    existing.approval_id != approval.id
                    or existing.destination_plan_hash != approval.destination_plan_hash
                ):
                    raise HTTPException(409, "EXECUTION_IDENTITY_CONFLICT")
                if (
                    existing.status == "BLOCKED"
                    and existing.error_category == "APPROVED_CONNECTOR_MODE_UNAVAILABLE"
                    and readiness == "READY"
                ):
                    existing.status = (
                        "READY" if entry["execution_kind"] == "AUTO" else "MANUAL_PENDING"
                    )
                    existing.error_category = None
                    existing.human_error = None
                    existing.result_summary = None
                    existing.retryable = False
                    existing.revision += 1
                    existing.updated_at = now
                elif existing.status == "READY" and readiness != "READY":
                    existing.status = "BLOCKED"
                    existing.error_category = readiness_reason or "DESTINATION_NOT_READY"
                    existing.human_error = _readiness_human_message(readiness_reason)
                    existing.result_summary = existing.human_error
                    existing.revision += 1
                    existing.updated_at = now
                if existing.execution_kind == "AUTO" and existing.status == "READY":
                    ready_auto.append(str(existing.id))
                continue
            manual_package = _manual_package(entry) if entry["execution_kind"] == "MANUAL" else None
            manual_package_hash = _hash(manual_package) if manual_package is not None else None
            if adapter is None and entry["execution_kind"] == "MANUAL":
                readiness = "BLOCKED"
                readiness_reason = readiness_reason or "MANUAL_PACKAGE_UNAVAILABLE"
            status = (
                "BLOCKED"
                if readiness != "READY"
                else "MANUAL_PENDING"
                if entry["execution_kind"] == "MANUAL"
                else "READY"
            )
            blocked_message = (
                _readiness_human_message(readiness_reason) if readiness != "READY" else None
            )
            execution = DestinationExecution(
                id=uuid.uuid4(),
                campaign_id=campaign.id,
                campaign_version_id=version.id,
                approval_id=approval.id,
                destination_key=entry["destination_key"],
                target_key=entry["target_key"],
                execution_kind=entry["execution_kind"],
                runtime_mode=entry.get("runtime_mode"),
                status=status,
                revision=1,
                approved_plan_hash=None,
                destination_plan_hash=approval.destination_plan_hash,
                idempotency_key=key,
                plan_entry=copy.deepcopy(entry),
                manual_package=manual_package,
                manual_package_hash=manual_package_hash,
                external_id=None,
                external_url=None,
                result_summary=(
                    "Manual action package is ready; no post was made."
                    if entry["execution_kind"] == "MANUAL" and readiness == "READY"
                    else blocked_message
                ),
                error_category=readiness_reason if readiness != "READY" else None,
                human_error=blocked_message,
                retryable=False,
                verification_status="NOT_REQUIRED",
                disposition={},
                created_at=now,
                updated_at=now,
            )
            db.add(execution)
            db.flush()
            created_any = True
            if entry["execution_kind"] == "AUTO" and readiness == "READY":
                ready_auto.append(str(execution.id))
            elif entry["execution_kind"] == "MANUAL" and readiness == "READY":
                declaration = adapter.declaration
                attempt = ExecutionAttempt(
                    id=uuid.uuid4(),
                    execution_id=execution.id,
                    attempt_number=1,
                    operation="PREPARE_MANUAL",
                    status="FINALIZED",
                    outcome="MANUAL_PACKAGE_READY",
                    started_at=now,
                    dispatched_at=None,
                    finished_at=now,
                    adapter_revision=declaration.adapter_revision,
                    request_fingerprint=_attempt_fingerprint(execution, 1, "PREPARE_MANUAL"),
                    external_id=None,
                    external_url=None,
                    error_category=None,
                    human_error=None,
                    retryable=False,
                    verification_status="NOT_REQUIRED",
                    evidence={"package_hash": manual_package_hash, "simulation": True},
                )
                db.add(attempt)
                db.flush()
                _audit(
                    db,
                    "MANUAL_PACKAGE_PREPARED",
                    actor_id=actor_id,
                    event_id=event.id,
                    campaign_id=campaign.id,
                    campaign_version_id=version.id,
                    approval_id=approval.id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    metadata={"destination_key": execution.destination_key},
                )
                manual_delivered = True
        if created_any:
            _audit(
                db,
                "EXECUTION_INITIALIZED",
                actor_id=actor_id,
                event_id=event.id,
                campaign_id=campaign.id,
                campaign_version_id=version.id,
                approval_id=approval.id,
                metadata={"target_count": len(approval.destination_plan["targets"])},
            )
        if manual_delivered:
            _set_campaign_state(campaign, "EXECUTING")
        elif campaign.state not in {"EXECUTING", "COMPLETE"}:
            _set_campaign_state(campaign, "APPROVED")
    return ready_auto


def _claim_auto_attempt(
    sessions: sessionmaker,
    execution_id,
    actor_id,
    *,
    adapters: dict,
    retry: bool,
):
    recover_expired_execution_attempts(sessions)
    with sessions() as db:
        initial = db.get(DestinationExecution, uuid.UUID(str(execution_id)))
        if initial is None:
            raise HTTPException(404, "DESTINATION_EXECUTION_NOT_FOUND")
        campaign_id, approval_id, version_id = (
            initial.campaign_id,
            initial.approval_id,
            initial.campaign_version_id,
        )
    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        execution = db.scalar(
            select(DestinationExecution)
            .where(DestinationExecution.id == uuid.UUID(str(execution_id)))
            .with_for_update()
        )
        if execution is None:
            raise HTTPException(404, "DESTINATION_EXECUTION_NOT_FOUND")
        if execution.execution_kind != "AUTO":
            raise HTTPException(409, "MANUAL_DESTINATION_CANNOT_AUTO_EXECUTE")
        if execution.status == "SUCCEEDED" or execution.status == "RUNNING":
            return None
        if execution.status in {"UNKNOWN", "MANUAL_PENDING", "MANUAL_DONE", "SKIPPED"}:
            raise HTTPException(409, "DESTINATION_NOT_RETRYABLE")
        if retry:
            if execution.status != "FAILED" or not execution.retryable:
                raise HTTPException(409, "DESTINATION_NOT_RETRYABLE")
        elif execution.status != "READY":
            return None
        approval = _find_approval(db, approval_id)
        version = _find_version(db, campaign, version_id)
        _assert_active_authority(campaign, event, approval, version)
        adapter = adapters.for_entry(execution.plan_entry)
        readiness, reason = adapters.readiness(execution.plan_entry)
        if adapter is None or execution.runtime_mode != execution.plan_entry.get("runtime_mode"):
            raise HTTPException(409, "APPROVED_CONNECTOR_MODE_UNAVAILABLE")
        if readiness != "READY":
            raise HTTPException(409, reason or "DESTINATION_NOT_READY")
        now = _now()
        number = _next_attempt_number(db, execution.id)
        previous_attempt = db.scalar(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.execution_id == execution.id)
            .order_by(ExecutionAttempt.attempt_number.desc())
            .limit(1)
        )
        attempt = ExecutionAttempt(
            id=uuid.uuid4(),
            execution_id=execution.id,
            attempt_number=number,
            operation="PUBLISH",
            status="RUNNING",
            outcome=None,
            started_at=now,
            lease_expires_at=now + EXECUTION_LEASE_DURATION,
            dispatched_at=now,
            finished_at=None,
            adapter_revision=adapter.declaration.adapter_revision,
            request_fingerprint=_attempt_fingerprint(execution, number, "PUBLISH"),
            external_id=None,
            external_url=None,
            error_category=None,
            human_error=None,
            retryable=None,
            verification_status="INCONCLUSIVE",
            evidence={
                "runtime_mode": execution.runtime_mode,
                "test_mode_adapter": adapter.declaration.adapter_revision.startswith("test-only-"),
                "possible_dispatch": True,
            },
        )
        db.add(attempt)
        db.flush()
        execution.status = "RUNNING"
        execution.revision += 1
        execution.retryable = False
        execution.disposition = {}
        execution.updated_at = now
        _set_campaign_state(campaign, "EXECUTING")
        _audit(
            db,
            "DESTINATION_ATTEMPT_STARTED",
            actor_id=actor_id,
            event_id=event.id,
            campaign_id=campaign.id,
            campaign_version_id=version.id,
            approval_id=approval.id,
            execution_id=execution.id,
            attempt_id=attempt.id,
            metadata={
                "destination_key": execution.destination_key,
                "attempt_number": number,
                "runtime_mode": execution.runtime_mode,
            },
        )
        db.flush()
        context = (
            execution.id,
            attempt.id,
            number,
            copy.deepcopy(execution.plan_entry),
            execution.idempotency_key,
            {
                "execution_id": str(execution.id),
                "event_id": str(event.id),
                "campaign_id": str(campaign.id),
                "approval_id": str(approval.id),
                "campaign_version_id": str(version.id),
                "external_id": execution.external_id
                or (previous_attempt.external_id if previous_attempt else None),
                "previous_attempt_evidence": copy.deepcopy(
                    previous_attempt.evidence or {} if previous_attempt else {}
                ),
            },
        )
    return context


def _verified_live_result(destination: str, result: NormalizedResult) -> bool:
    evidence = result.evidence or {}
    if destination == "EVENTBRITE":
        return bool(
            evidence.get("adapter") == EVENTBRITE_ADAPTER_REVISION
            and evidence.get("verification") == "VERIFIED_LIVE"
            and result.external_id
            and result.external_id.isdigit()
            and result.external_url
            and EVENT_URL.match(result.external_url)
        )
    if destination == "MEETUP":
        return bool(
            evidence.get("adapter") == MEETUP_ADAPTER_REVISION
            and evidence.get("verification") == "VERIFIED_LIVE"
            and result.external_id
            and result.external_id.isdigit()
            and result.external_url
            and MEETUP_EVENT_URL.fullmatch(result.external_url)
        )
    if destination == "LINKEDIN_ORGANIZATION":
        from app.linkedin import ADAPTER_REVISION as LINKEDIN_ADAPTER_REVISION
        from app.linkedin import POST_ID

        return bool(
            evidence.get("adapter") == LINKEDIN_ADAPTER_REVISION
            and evidence.get("verification") == "VERIFIED_LIVE"
            and isinstance(result.external_id, str)
            and POST_ID.fullmatch(result.external_id)
            and result.external_url is None
        )
    if destination == "INSTAGRAM":
        from app.instagram import MEDIA_ID

        return bool(
            evidence.get("adapter") == INSTAGRAM_ADAPTER_REVISION
            and evidence.get("verification") == "VERIFIED_LIVE"
            and isinstance(result.external_id, str)
            and MEDIA_ID.fullmatch(result.external_id)
            and isinstance(result.external_url, str)
            and result.external_url.startswith("https://www.instagram.com/p/")
        )
    return False


def _normalize_execute_result(
    result: NormalizedResult, runtime_mode: str, destination: str | None = None
) -> tuple[str, dict]:
    if result.outcome == "PUBLISHED":
        evidence = result.evidence or {}
        if runtime_mode == "SIMULATED":
            valid_id = bool(result.external_id and result.external_id.startswith("simulated:"))
            valid_url = bool(
                result.external_url and result.external_url.startswith("https://simulated.invalid/")
            )
            evidence_valid = evidence.get("creation_receipt") == "VERIFIED"
            summary = "Published in practice mode; no external platform was contacted."
            verification_status = "SIMULATED"
        elif _verified_live_result(destination or "", result):
            valid_id = valid_url = evidence_valid = True
            summary = (
                "Published on Eventbrite and verified."
                if destination == "EVENTBRITE"
                else "Published on Meetup and verified."
                if destination == "MEETUP"
                else "Published on LinkedIn and verified."
                if destination == "LINKEDIN_ORGANIZATION"
                else "Published on Instagram and verified."
            )
            verification_status = "VERIFIED"
        else:
            valid_id = bool(result.external_id and result.external_id.startswith("test-live:"))
            valid_url = bool(
                result.external_url
                and result.external_url.startswith("https://eventbrite.mock.invalid/")
            )
            evidence_valid = (
                evidence.get("test_only_adapter") is True and evidence.get("network_calls") == 0
            )
            summary = "Completed through the no-network LIVE-mode test adapter."
            verification_status = "TEST_MOCKED"
        if not valid_id or not valid_url or not evidence_valid:
            return "UNKNOWN", {
                "outcome": "UNCERTAIN",
                "human_error": "The destination result did not provide required success evidence.",
                "error_category": "AMBIGUOUS_OUTCOME",
                "retryable": False,
                "verification_status": "INCONCLUSIVE",
                "evidence": {"simulation": True, "success_evidence_invalid": True},
            }
        return "SUCCEEDED", {
            "outcome": "PUBLISHED",
            "external_id": result.external_id,
            "external_url": result.external_url,
            "result_summary": summary,
            "error_category": None,
            "human_error": None,
            "retryable": False,
            "verification_status": verification_status,
            "evidence": evidence,
        }
    if result.outcome == "NOT_CREATED":
        return "FAILED", {
            "outcome": "NOT_CREATED",
            "external_id": None,
            "external_url": None,
            "result_summary": (result.evidence or {}).get("result_summary")
            or "The destination confirmed no item was created.",
            "error_category": result.error_category or "REMOTE_REJECTED",
            "human_error": result.human_error or "The destination did not create this item.",
            "retryable": bool(result.retryable),
            "verification_status": result.verification_status,
            "evidence": result.evidence or {},
        }
    if result.outcome == "BLOCKED":
        return "BLOCKED", {
            "outcome": "BLOCKED",
            "external_id": result.external_id,
            "external_url": result.external_url,
            "result_summary": result.human_error or "Meetup cannot publish this version yet.",
            "error_category": result.error_category or "DESTINATION_NOT_READY",
            "human_error": result.human_error or "Meetup cannot publish this version yet.",
            "retryable": False,
            "verification_status": result.verification_status,
            "evidence": result.evidence or {},
        }
    return "UNKNOWN", {
        "outcome": "UNCERTAIN",
        # Preserve a provider-issued identity when a write may have succeeded;
        # reconciliation can read that exact resource instead of guessing.
        "external_id": result.external_id,
        "external_url": None,
        "result_summary": "The result is uncertain; verify it before any retry.",
        "error_category": "AMBIGUOUS_OUTCOME",
        "human_error": result.human_error or "The simulated outcome is uncertain.",
        "retryable": False,
        "verification_status": "INCONCLUSIVE",
        "evidence": result.evidence or {},
    }


def _finalize_auto_attempt(sessions: sessionmaker, context, result: NormalizedResult | None):
    execution_id, attempt_id, number, plan_entry, _key, _meta = context
    if result is None:
        status = "UNKNOWN"
        normalized = {
            "outcome": "UNCERTAIN",
            "external_id": None,
            "external_url": None,
            "result_summary": (
                "The destination adapter did not return a result; verify before retrying."
            ),
            "error_category": "AMBIGUOUS_OUTCOME",
            "human_error": "The destination result is uncertain.",
            "retryable": False,
            "verification_status": "INCONCLUSIVE",
            "evidence": {
                "runtime_mode": plan_entry.get("runtime_mode"),
                "adapter_response_missing": True,
            },
        }
    else:
        status, normalized = _normalize_execute_result(
            result,
            plan_entry.get("runtime_mode", "SIMULATED"),
            plan_entry.get("destination_key"),
        )
    with sessions.begin() as db:
        execution = db.get(DestinationExecution, execution_id)
        campaign, event = _find_campaign(db, execution.campaign_id, lock=True)
        execution = db.scalar(
            select(DestinationExecution)
            .where(DestinationExecution.id == execution_id)
            .with_for_update()
        )
        attempt = db.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.id == attempt_id).with_for_update()
        )
        if attempt.status == "FINALIZED":
            return
        now = _now()
        attempt.status = "FINALIZED"
        attempt.outcome = normalized["outcome"]
        attempt.finished_at = now
        attempt.lease_expires_at = None
        attempt.external_id = normalized.get("external_id")
        attempt.external_url = normalized.get("external_url")
        attempt.error_category = normalized.get("error_category")
        attempt.human_error = normalized.get("human_error")
        attempt.retryable = normalized.get("retryable")
        attempt.verification_status = normalized.get("verification_status", "INCONCLUSIVE")
        attempt.evidence = normalized.get("evidence") or {}
        execution.status = status
        execution.external_id = normalized.get("external_id")
        execution.external_url = normalized.get("external_url")
        execution.result_summary = normalized.get("result_summary")
        execution.error_category = normalized.get("error_category")
        execution.human_error = normalized.get("human_error")
        execution.retryable = bool(normalized.get("retryable"))
        execution.verification_status = normalized.get("verification_status", "INCONCLUSIVE")
        execution.revision += 1
        execution.updated_at = now
        approval = db.get(Approval, execution.approval_id)
        _audit(
            db,
            f"DESTINATION_{normalized['outcome']}",
            actor_type="ADAPTER",
            event_id=event.id,
            campaign_id=campaign.id,
            campaign_version_id=execution.campaign_version_id,
            approval_id=approval.id,
            execution_id=execution.id,
            attempt_id=attempt.id,
            metadata={
                "destination_key": execution.destination_key,
                "attempt_number": number,
                "retryable": execution.retryable,
                "verification_status": execution.verification_status,
            },
        )
        if campaign.active_approval_id == approval.id:
            _refresh_campaign_state(db, campaign, approval)


def execute_one(
    sessions: sessionmaker,
    execution_id,
    actor_id,
    adapters: dict,
    *,
    retry: bool = False,
) -> None:
    context = _claim_auto_attempt(sessions, execution_id, actor_id, adapters=adapters, retry=retry)
    if context is None:
        return
    _execution_id, _attempt_id, number, plan_entry, idempotency_key, meta = context
    adapter = adapters.for_entry(plan_entry)
    try:
        if getattr(adapter, "accepts_context", False):
            result = adapter.execute(plan_entry, idempotency_key, number, context=meta)
        else:
            result = adapter.execute(plan_entry, idempotency_key, number)
    except Exception:
        # A dispatched operation with an adapter exception is ambiguous by default.
        result = None
    _finalize_auto_attempt(sessions, context, result)


def start_execution(sessions: sessionmaker, approval_id, actor_id, adapters: dict) -> dict:
    ready_ids = initialize_execution(sessions, approval_id, actor_id, adapters)
    for execution_id in ready_ids:
        execute_one(sessions, execution_id, actor_id, adapters)
    approval = _find_approval_by_id(sessions, approval_id)
    return distribution_view(sessions, approval.campaign_id)


def _find_approval_by_id(sessions: sessionmaker, approval_id) -> Approval:
    with sessions() as db:
        return copy.copy(_find_approval(db, approval_id))


def retry_execution(sessions: sessionmaker, execution_id, actor_id, adapters: dict) -> dict:
    execute_one(sessions, execution_id, actor_id, adapters, retry=True)
    with sessions() as db:
        execution = db.get(DestinationExecution, uuid.UUID(str(execution_id)))
        if execution is None:
            raise HTTPException(404, "DESTINATION_EXECUTION_NOT_FOUND")
        return distribution_view(sessions, execution.campaign_id)


def _claim_reconciliation(
    sessions: sessionmaker,
    execution_id,
    actor_id,
    adapters: DestinationAdapterRegistry,
):
    recover_expired_execution_attempts(sessions)
    with sessions() as db:
        initial = db.get(DestinationExecution, uuid.UUID(str(execution_id)))
        if initial is None:
            raise HTTPException(404, "DESTINATION_EXECUTION_NOT_FOUND")
        campaign_id = initial.campaign_id
    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        execution = db.scalar(
            select(DestinationExecution)
            .where(DestinationExecution.id == uuid.UUID(str(execution_id)))
            .with_for_update()
        )
        if execution.status != "UNKNOWN":
            return None
        adapter = adapters.for_entry(execution.plan_entry)
        readiness, _reason = adapters.readiness(execution.plan_entry)
        if (
            adapter is None
            or not adapter.declaration.can_verify
            or readiness != "READY"
            or execution.runtime_mode != execution.plan_entry.get("runtime_mode")
        ):
            raise HTTPException(409, "RECONCILIATION_UNAVAILABLE")
        if db.scalar(
            select(ExecutionAttempt.id).where(
                ExecutionAttempt.execution_id == execution.id,
                ExecutionAttempt.status == "RUNNING",
            )
        ):
            return None
        approval = _find_approval(db, execution.approval_id)
        version = _find_version(db, campaign, execution.campaign_version_id)
        _check_approval_integrity(approval, version)
        previous_attempt = db.scalar(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.execution_id == execution.id)
            .order_by(ExecutionAttempt.attempt_number.desc())
            .limit(1)
        )
        number = _next_attempt_number(db, execution.id)
        now = _now()
        attempt = ExecutionAttempt(
            id=uuid.uuid4(),
            execution_id=execution.id,
            attempt_number=number,
            operation="RECONCILE",
            status="RUNNING",
            outcome=None,
            started_at=now,
            lease_expires_at=now + EXECUTION_LEASE_DURATION,
            dispatched_at=now,
            finished_at=None,
            adapter_revision=adapter.declaration.adapter_revision,
            request_fingerprint=_attempt_fingerprint(execution, number, "RECONCILE"),
            external_id=None,
            external_url=None,
            error_category=None,
            human_error=None,
            retryable=None,
            verification_status="INCONCLUSIVE",
            evidence={
                "runtime_mode": execution.runtime_mode,
                "operation_is_verification_only": True,
            },
        )
        db.add(attempt)
        db.flush()
        _audit(
            db,
            "RECONCILIATION_STARTED",
            actor_id=actor_id,
            event_id=event.id,
            campaign_id=campaign.id,
            campaign_version_id=version.id,
            approval_id=approval.id,
            execution_id=execution.id,
            attempt_id=attempt.id,
            metadata={"destination_key": execution.destination_key},
        )
        return (
            execution.id,
            attempt.id,
            number,
            copy.deepcopy(execution.plan_entry),
            execution.idempotency_key,
            {
                "execution_id": str(execution.id),
                "event_id": str(event.id),
                "campaign_id": str(campaign.id),
                "approval_id": str(approval.id),
                "campaign_version_id": str(version.id),
                "external_id": execution.external_id
                or (previous_attempt.external_id if previous_attempt else None),
                "previous_attempt_evidence": copy.deepcopy(
                    previous_attempt.evidence or {} if previous_attempt else {}
                ),
            },
        )


def reconcile_execution(
    sessions: sessionmaker,
    execution_id,
    actor_id,
    adapters: DestinationAdapterRegistry,
) -> dict:
    context = _claim_reconciliation(sessions, execution_id, actor_id, adapters)
    if context:
        execution_uuid, attempt_id, number, plan_entry, key, meta = context
        adapter = adapters.for_entry(plan_entry)
        try:
            if getattr(adapter, "accepts_context", False):
                result = adapter.verify(plan_entry, key, context=meta)
            else:
                result = adapter.verify(plan_entry, key)
        except Exception:
            result = NormalizedResult(
                outcome="VERIFICATION_RESULT",
                verification_status="INCONCLUSIVE",
                verification_finding="INCONCLUSIVE",
                error_category="AMBIGUOUS_OUTCOME",
                human_error="The destination result remains uncertain.",
                evidence={"simulation": True, "verification_failed": True},
            )
        with sessions.begin() as db:
            execution = db.get(DestinationExecution, execution_uuid)
            campaign, event = _find_campaign(db, execution.campaign_id, lock=True)
            execution = db.scalar(
                select(DestinationExecution)
                .where(DestinationExecution.id == execution_uuid)
                .with_for_update()
            )
            attempt = db.scalar(
                select(ExecutionAttempt).where(ExecutionAttempt.id == attempt_id).with_for_update()
            )
            if attempt.status == "FINALIZED":
                pass
            else:
                finding = result.verification_finding or "INCONCLUSIVE"
                now = _now()
                attempt.status = "FINALIZED"
                attempt.outcome = "VERIFICATION_RESULT"
                attempt.finished_at = now
                attempt.lease_expires_at = None
                attempt.external_id = result.external_id
                attempt.external_url = result.external_url
                attempt.error_category = result.error_category
                attempt.human_error = result.human_error
                attempt.retryable = result.retryable
                attempt.verification_status = result.verification_status
                attempt.evidence = result.evidence or {}
                execution.revision += 1
                execution.updated_at = now
                if (
                    finding == "FOUND_MATCHING"
                    and result.external_id
                    and (
                        execution.runtime_mode == "SIMULATED"
                        and result.external_id.startswith("simulated:")
                        and result.external_url
                        and result.external_url.startswith("https://simulated.invalid/")
                        or execution.runtime_mode == "LIVE"
                        and _verified_live_result(execution.destination_key, result)
                        or execution.runtime_mode == "LIVE"
                        and result.external_id.startswith("test-live:")
                        and result.external_url
                        and result.external_url.startswith("https://eventbrite.mock.invalid/")
                        and (result.evidence or {}).get("test_only_adapter") is True
                        and (result.evidence or {}).get("network_calls") == 0
                    )
                ):
                    execution.status = "SUCCEEDED"
                    execution.external_id = result.external_id
                    execution.external_url = result.external_url
                    execution.result_summary = (
                        "Published on LinkedIn and verified."
                        if execution.destination_key == "LINKEDIN_ORGANIZATION"
                        else f"Published on {execution.destination_key.title()} and verified."
                        if _verified_live_result(execution.destination_key, result)
                        else "Verified using the no-network LIVE-mode test adapter."
                        if execution.runtime_mode == "LIVE"
                        else "Simulated result verified; no external platform was contacted."
                    )
                    execution.error_category = None
                    execution.human_error = None
                    execution.retryable = False
                    execution.verification_status = result.verification_status
                elif finding == "CONFIRMED_ABSENT":
                    execution.status = "FAILED"
                    execution.external_id = None
                    execution.external_url = None
                    execution.result_summary = (
                        result.human_error or "Reconciliation confirmed that no item was created."
                    )
                    execution.error_category = "TRANSIENT_CONFIRMED_NO_EFFECT"
                    execution.human_error = (
                        result.human_error or "No item exists; a deliberate retry is safe."
                    )
                    execution.retryable = True
                    execution.verification_status = "VERIFIED"
                else:
                    execution.status = "UNKNOWN"
                    execution.result_summary = "The result remains uncertain; do not retry yet."
                    execution.error_category = "AMBIGUOUS_OUTCOME"
                    execution.human_error = "The check could not determine the result."
                    execution.retryable = False
                    execution.verification_status = "INCONCLUSIVE"
                approval = db.get(Approval, execution.approval_id)
                _audit(
                    db,
                    f"RECONCILIATION_{finding}",
                    actor_id=actor_id,
                    event_id=event.id,
                    campaign_id=campaign.id,
                    campaign_version_id=execution.campaign_version_id,
                    approval_id=approval.id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    metadata={
                        "destination_key": execution.destination_key,
                        "attempt_number": number,
                        "finding": finding,
                    },
                )
                if campaign.active_approval_id == approval.id:
                    _refresh_campaign_state(db, campaign, approval)
    with sessions() as db:
        execution = db.get(DestinationExecution, uuid.UUID(str(execution_id)))
        if execution is None:
            raise HTTPException(404, "DESTINATION_EXECUTION_NOT_FOUND")
        return distribution_view(sessions, execution.campaign_id)


def _human_disposition(
    sessions: sessionmaker,
    execution_id,
    actor_id,
    action: str,
    note: str | None = None,
) -> dict:
    recover_expired_execution_attempts(sessions)
    try:
        parsed_id = uuid.UUID(str(execution_id))
    except ValueError:
        raise HTTPException(404, "DESTINATION_EXECUTION_NOT_FOUND") from None
    with sessions() as db:
        initial = db.get(DestinationExecution, parsed_id)
        if initial is None:
            raise HTTPException(404, "DESTINATION_EXECUTION_NOT_FOUND")
        campaign_id = initial.campaign_id
    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        execution = db.scalar(
            select(DestinationExecution)
            .where(DestinationExecution.id == parsed_id)
            .with_for_update()
        )
        approval = _find_approval(db, execution.approval_id)
        version = _find_version(db, campaign, execution.campaign_version_id)
        _assert_active_authority(campaign, event, approval, version)
        expected = {
            "complete_manual": (
                execution.execution_kind == "MANUAL" and execution.status == "MANUAL_PENDING",
                "MANUAL_DONE",
                "MANUAL_COMPLETION",
            ),
            "handoff_manual": (
                execution.execution_kind == "MANUAL" and execution.status == "MANUAL_PENDING",
                "MANUAL_PENDING",
                "MANUAL_HANDOFF",
            ),
            "skip": (
                execution.status in {"MANUAL_PENDING", "FAILED", "BLOCKED", "READY"},
                "SKIPPED",
                "SKIPPED",
            ),
            "ack_failure": (
                execution.status in {"FAILED", "BLOCKED"},
                execution.status,
                "ACKNOWLEDGED_FAILURE",
            ),
            "ack_uncertain": (execution.status == "UNKNOWN", "UNKNOWN", "ABANDONED_UNCERTAIN"),
        }.get(action)
        if expected is None or not expected[0]:
            raise HTTPException(409, "DESTINATION_DISPOSITION_NOT_ALLOWED")
        _valid_status, new_status, disposition_type = expected
        if execution.disposition.get("type") == disposition_type:
            return distribution_view(sessions, campaign.id, recover=False)
        now = _now()
        execution.status = new_status
        execution.disposition = {
            "type": disposition_type,
            "actor_id": str(actor_id),
            "occurred_at": now.isoformat(),
            "note": note.strip() if note else None,
            "result_fingerprint": execution.idempotency_key,
        }
        execution.revision += 1
        execution.updated_at = now
        number = _next_attempt_number(db, execution.id)
        operation = (
            "ACK_MANUAL"
            if action.startswith("complete_manual") or action.startswith("handoff")
            else "DISPOSITION"
        )
        attempt = ExecutionAttempt(
            id=uuid.uuid4(),
            execution_id=execution.id,
            attempt_number=number,
            operation=operation,
            status="FINALIZED",
            outcome="MANUAL_ATTESTED" if action == "complete_manual" else "BLOCKED",
            started_at=now,
            dispatched_at=None,
            finished_at=now,
            adapter_revision="human-action-v1",
            request_fingerprint=_attempt_fingerprint(execution, number, operation),
            external_id=None,
            external_url=None,
            error_category=execution.error_category,
            human_error=note.strip()[:500] if note else None,
            retryable=execution.retryable,
            verification_status=(
                "HUMAN_ATTESTED" if action == "complete_manual" else execution.verification_status
            ),
            evidence={"actor_id": str(actor_id), "disposition": disposition_type},
        )
        db.add(attempt)
        db.flush()
        _audit(
            db,
            disposition_type,
            actor_id=actor_id,
            event_id=event.id,
            campaign_id=campaign.id,
            campaign_version_id=version.id,
            approval_id=approval.id,
            execution_id=execution.id,
            attempt_id=attempt.id,
            reason=note,
            metadata={"destination_key": execution.destination_key},
        )
        _refresh_campaign_state(db, campaign, approval)
        if action == "complete_manual":
            execution.result_summary = (
                "The operator marked the Facebook Groups manual action complete."
            )
            execution.verification_status = "HUMAN_ATTESTED"
        elif action == "handoff_manual":
            execution.result_summary = (
                "Manual package handed off; external posting remains unverified."
            )
        elif action == "ack_failure":
            execution.result_summary = "Failure accounted for by an authorized user."
        elif action == "ack_uncertain":
            execution.result_summary = "Uncertainty acknowledged; possible external effect remains."
        elif action == "skip":
            execution.result_summary = "Destination action explicitly skipped."
        db.flush()
    return distribution_view(sessions, campaign_id)


def complete_manual(sessions, execution_id, actor_id, note=None):
    return _human_disposition(sessions, execution_id, actor_id, "complete_manual", note)


def handoff_manual(sessions, execution_id, actor_id, note=None):
    return _human_disposition(sessions, execution_id, actor_id, "handoff_manual", note)


def skip_execution(sessions, execution_id, actor_id, note=None):
    return _human_disposition(sessions, execution_id, actor_id, "skip", note)


def acknowledge_failure(sessions, execution_id, actor_id, note=None):
    return _human_disposition(sessions, execution_id, actor_id, "ack_failure", note)


def acknowledge_uncertain(sessions, execution_id, actor_id, note=None):
    return _human_disposition(sessions, execution_id, actor_id, "ack_uncertain", note)


def _changed_paths(before, after, prefix="") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                result.append(path)
            else:
                result.extend(_changed_paths(before[key], after[key], path))
        return result
    return [] if before == after else [prefix]


def prepare_for_successor(db: Session, campaign: Campaign, event: Event, actor_id) -> None:
    """Withdraw exact active authorization before appending a successor version."""
    if campaign.active_approval_id is None:
        return
    approval = db.get(Approval, campaign.active_approval_id)
    if approval is None:
        raise HTTPException(409, "ACTIVE_APPROVAL_NOT_FOUND")
    rows = db.scalars(
        select(DestinationExecution)
        .where(DestinationExecution.approval_id == approval.id)
        .with_for_update()
    ).all()
    if any(
        value.status == "RUNNING"
        or (value.status == "UNKNOWN" and value.disposition.get("type") != "ABANDONED_UNCERTAIN")
        for value in rows
    ):
        raise HTTPException(409, "EXECUTION_UNRESOLVED_BEFORE_REVISION")
    campaign.active_approval_id = None
    for execution in rows:
        if execution.status in {"READY", "MANUAL_PENDING", "BLOCKED"}:
            execution.status = "SKIPPED"
            execution.disposition = {
                "type": "SUPERSEDED",
                "actor_id": str(actor_id),
                "occurred_at": _now().isoformat(),
                "note": "A successor Campaign Version was created.",
            }
            execution.result_summary = "Superseded by a newer Campaign Version."
            execution.retryable = False
            execution.revision += 1
            execution.updated_at = _now()
    _audit(
        db,
        "APPROVAL_SUPERSEDED_BY_SUCCESSOR",
        actor_id=actor_id,
        event_id=event.id,
        campaign_id=campaign.id,
        campaign_version_id=approval.campaign_version_id,
        approval_id=approval.id,
        metadata={"superseded_units": len(rows)},
    )


def invalidate_material_event_approvals(
    db: Session, event: Event, before_snapshot: dict, actor_id
) -> None:
    """Atomically withdraw only approvals whose version no longer matches material facts."""
    after_snapshot = material_event_snapshot(event)
    before_projection = business_event_projection(before_snapshot)
    after_projection = business_event_projection(after_snapshot)
    if before_projection == after_projection:
        return
    changed_paths = _changed_paths(before_projection, after_projection)
    campaigns = db.scalars(
        select(Campaign)
        .where(Campaign.event_id == event.id, Campaign.active_approval_id.is_not(None))
        .order_by(Campaign.id)
        .with_for_update()
    ).all()
    for campaign in campaigns:
        approval = db.get(Approval, campaign.active_approval_id)
        version = db.get(CampaignVersion, approval.campaign_version_id) if approval else None
        if not approval or not version or not is_stale(event, version):
            continue
        rows = db.scalars(
            select(DestinationExecution)
            .where(DestinationExecution.approval_id == approval.id)
            .with_for_update()
        ).all()
        if any(
            value.status == "RUNNING"
            or (
                value.status == "UNKNOWN" and value.disposition.get("type") != "ABANDONED_UNCERTAIN"
            )
            for value in rows
        ):
            raise HTTPException(409, "EVENT_CHANGE_BLOCKED_BY_UNRESOLVED_EXECUTION")
        campaign.active_approval_id = None
        _set_campaign_state(campaign, "DRAFT")
        for execution in rows:
            if execution.status in {"READY", "MANUAL_PENDING", "BLOCKED"}:
                execution.status = "SKIPPED"
                execution.disposition = {
                    "type": "SUPERSEDED",
                    "actor_id": str(actor_id),
                    "occurred_at": _now().isoformat(),
                    "note": "Material Event details changed.",
                }
                execution.result_summary = "Superseded by a material Event change."
                execution.retryable = False
                execution.revision += 1
                execution.updated_at = _now()
        _audit(
            db,
            "APPROVAL_SUPERSEDED_EVENT_CHANGE",
            actor_id=actor_id,
            event_id=event.id,
            campaign_id=campaign.id,
            campaign_version_id=version.id,
            approval_id=approval.id,
            reason="Material Event facts changed.",
            metadata={"changed_paths": changed_paths[:40]},
        )


def distribution_result(sessions, execution_id):
    with sessions() as db:
        execution = db.get(DestinationExecution, uuid.UUID(str(execution_id)))
        if execution is None:
            raise HTTPException(404, "DESTINATION_EXECUTION_NOT_FOUND")
        return distribution_view(sessions, execution.campaign_id)


class CampaignExecutionService:
    def __init__(
        self,
        settings,
        database=None,
        eventbrite_http_client=None,
        meetup_http_client=None,
        linkedin_http_client=None,
        linkedin_credentials=None,
        instagram_http_client=None,
        instagram_credentials=None,
    ):
        if eventbrite_http_client is not None and settings.environment != "test":
            raise RuntimeError("Eventbrite transport substitution is permitted only in tests")
        if meetup_http_client is not None and settings.environment != "test":
            raise RuntimeError("Meetup transport substitution is permitted only in tests")
        if linkedin_http_client is not None and settings.environment != "test":
            raise RuntimeError("LinkedIn transport substitution is permitted only in tests")
        if (
            linkedin_credentials is not None
            and settings.environment != "test"
            and not isinstance(linkedin_credentials, VaultLinkedInCredentials)
        ):
            raise RuntimeError("LinkedIn credential substitution is permitted only in tests")
        if instagram_http_client is not None and settings.environment != "test":
            raise RuntimeError("Instagram transport substitution is permitted only in tests")
        if (
            instagram_credentials is not None
            and settings.environment != "test"
            and not isinstance(instagram_credentials, VaultInstagramCredentials)
        ):
            raise RuntimeError("Instagram credential substitution is permitted only in tests")
        scenarios = (
            SimulationScenarioController(settings.environment)
            if settings.environment == "test"
            else None
        )
        self.settings = settings
        self.database = database
        self.eventbrite_http_client = eventbrite_http_client
        self.meetup_http_client = meetup_http_client
        self.linkedin_http_client = linkedin_http_client
        self.linkedin_credentials = linkedin_credentials
        self.instagram_http_client = instagram_http_client
        self.instagram_credentials = instagram_credentials
        # Test-only switch: the real adapter over a mocked transport vs. the WP1I-A double.
        self.live_connector_enabled = eventbrite_http_client is not None
        self.scenarios = scenarios
        self.rebuild()

    def _eventbrite_live(self):
        # Tests without an Eventbrite HTTP mock keep the WP1I-A no-network double.
        if self.database is None or (
            self.settings.environment == "test"
            and (self.eventbrite_http_client is None or not self.live_connector_enabled)
        ):
            return None
        client = EventbriteClient(
            PrivateTokenCredentials(self.settings), http_client=self.eventbrite_http_client
        )
        return EventbriteLiveAdapter(
            self.settings, EventbriteRepository(self.database.sessions, self.settings), client
        )

    def _meetup_live(self):
        if self.database is None or (
            self.settings.environment == "test" and self.meetup_http_client is None
        ):
            return None
        credentials = MeetupJWTTokenProvider(self.settings, http_client=self.meetup_http_client)
        client = MeetupGraphQLClient(credentials, http_client=self.meetup_http_client)
        return MeetupLiveAdapter(self.settings, MeetupRepository(self.database.sessions), client)

    def _linkedin_live(self):
        if self.database is None or (
            self.settings.environment == "test"
            and (self.linkedin_http_client is None or self.linkedin_credentials is None)
        ):
            return None
        credentials = self.linkedin_credentials or UnavailableLinkedInCredentials()
        client = LinkedInRestClient(
            self.settings,
            http_client=self.linkedin_http_client,
            on_authentication_failure=_reauthorization_callback(credentials),
        )
        return LinkedInLiveAdapter(
            self.settings,
            LinkedInRepository(self.database.sessions, self.settings),
            credentials,
            client,
        )

    def _instagram_live(self):
        if self.database is None or (
            self.settings.environment == "test"
            and (self.instagram_http_client is None or self.instagram_credentials is None)
        ):
            return None
        credentials = self.instagram_credentials or UnavailableInstagramCredentials()
        client = InstagramAPIClient(
            self.settings,
            http_client=self.instagram_http_client,
            on_authentication_failure=_reauthorization_callback(credentials),
        )
        return InstagramLiveAdapter(
            self.settings,
            InstagramRepository(self.database.sessions, self.settings),
            credentials,
            client,
        )

    def rebuild(self) -> None:
        self.adapters = DestinationAdapterRegistry(
            self.settings,
            self.scenarios,
            live_factory=self._eventbrite_live,
            meetup_live_factory=self._meetup_live,
            linkedin_live_factory=self._linkedin_live,
            instagram_live_factory=self._instagram_live,
        )

    def linkedin_status(self):
        credentials = self.linkedin_credentials or UnavailableLinkedInCredentials()
        client = LinkedInRestClient(
            self.settings,
            http_client=self.linkedin_http_client,
            on_authentication_failure=_reauthorization_callback(credentials),
        )
        try:
            from app.linkedin import live_readiness

            return live_readiness(self.settings, credentials, client)
        finally:
            client.close()

    def instagram_status(self):
        credentials = self.instagram_credentials or UnavailableInstagramCredentials()
        client = InstagramAPIClient(
            self.settings,
            http_client=self.instagram_http_client,
            on_authentication_failure=_reauthorization_callback(credentials),
        )
        try:
            from app.instagram import live_readiness

            return live_readiness(self.settings, credentials, client)
        finally:
            client.close()

    def eventbrite_client(self):
        if self.database is None or (
            self.settings.environment == "test" and self.eventbrite_http_client is None
        ):
            return None
        return EventbriteClient(
            PrivateTokenCredentials(self.settings), http_client=self.eventbrite_http_client
        )

    def meetup_client(self):
        if self.settings.environment == "test" and self.meetup_http_client is None:
            return None
        credentials = MeetupJWTTokenProvider(self.settings, http_client=self.meetup_http_client)
        return MeetupGraphQLClient(credentials, http_client=self.meetup_http_client)

    def preview(self, sessions, campaign_id):
        return approval_preview(sessions, campaign_id, self.adapters)

    def approve(self, sessions, campaign_id, actor_id, **kwargs):
        return approve_version(sessions, campaign_id, actor_id, self.adapters, **kwargs)

    def start(self, sessions, approval_id, actor_id):
        return start_execution(sessions, approval_id, actor_id, self.adapters)

    def retry(self, sessions, execution_id, actor_id):
        return retry_execution(sessions, execution_id, actor_id, self.adapters)

    def reconcile(self, sessions, execution_id, actor_id):
        return reconcile_execution(sessions, execution_id, actor_id, self.adapters)
