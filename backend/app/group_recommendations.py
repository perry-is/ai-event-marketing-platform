"""Facebook Group eligibility, explainable recommendations and human selection.

Pipeline: catalog records -> deterministic hard filters -> deterministic signals/tiers ->
optional model ranking of a bounded shortlist -> validation -> the operator's explicit decisions.
Selections become distribution intent only when frozen into a CampaignVersion snapshot; a
changed selection after approval withdraws that approval and needs a successor version.
"""

import copy
import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.group_catalog import STATE_BY_NAME, US_STATES, tokens
from app.group_ranker import (
    MAX_SHORTLIST,
    PROMPT_VERSION,
    GroupRanker,
    RankingFailed,
    deterministic_ranking,
    model_payload,
)
from app.models import (
    Campaign,
    CampaignVersion,
    Event,
    FacebookGroupRecord,
    GroupDecision,
    GroupRecommendation,
)

LOGGER = logging.getLogger("event_marketing.groups")
MAX_MODEL_CANDIDATES = 30
SNAPSHOT_KEY = "facebook_group_targets"
REVISION_TARGET = "facebook_group_targets"

GENERIC_TOPIC_WORDS = {
    "business", "connect", "networking", "network", "event", "events", "community", "group",
    "learn", "lunch",
}  # fmt: skip
OFF_AUDIENCE_CATEGORIES = {"housing", "photography", "spiritual", "job"}
OFF_AUDIENCE_WORDS = {
    "buy", "sell", "swap", "trade", "garage", "sale", "free", "classifieds", "munchies",
    "rent", "rentals", "yard",
}  # fmt: skip
EXCLUSION_TEXT = {
    "UNRESOLVED_IDENTITY": "Link is a Facebook share link, so the exact group isn't confirmed.",
    "UNRECOGNIZED_URL": "The recorded link isn't a recognizable Facebook group link.",
    "IDENTITY_CONFLICT_NEEDS_REVIEW": "Conflicting records in the Group Master need review.",
    "DUPLICATE_SOURCE_ROW": "Duplicate of another row in the Group Master.",
    "CATALOG_EXCLUDED": "Excluded in the Group Master review.",
    "SOURCE_SUPERSEDED": "No longer present in the latest Group Master import.",
    "EVENT_PROMOTION_RESTRICTED": "The Group Master says event promotion isn't allowed here.",
    "GEOGRAPHY_MISMATCH": "Group is in a different state from this in-person event.",
}
CAUTION_TEXT = {
    "GEOGRAPHY_UNCERTAIN": "Location in the Group Master is unclear.",
    "AUDIENCE_MISMATCH": (
        "Audience may not be business-focused (for example buy/sell, housing or jobs)."
    ),
    "POSTING_RULES": "Has posting rules in the Group Master — check them before sharing.",
    "ADMIN_CHANGE_RESTRICTION": "Group Master says not to change this group's banner or name.",
    "OWNERSHIP_NOTE": "Marked “Not my group” in the Group Master.",
}
TIER_ORDER = {"BEST_FIT": 0, "GOOD_FIT": 1, "POSSIBLE_FIT": 2}


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Event context ---------------------------------------------------------------------------


def _states_in_text(text: str) -> set[str]:
    found = {m.group(1) for m in re.finditer(r",\s*([A-Z]{2})\b", text) if m.group(1) in US_STATES}
    lowered = text.casefold()
    for name, code in STATE_BY_NAME.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            found.add(code)
    return found


def event_context(event_snapshot: dict, content: dict | None) -> dict:
    """Only accepted Event facts and the reviewed summary; no notes, sources or sessions."""
    facts = event_snapshot.get("facts") or {}
    location = facts.get("location") or {}
    place = (
        ", ".join(value for value in (location.get("venue"), location.get("address")) if value)
        or None
    )
    return {
        "title": event_snapshot.get("title"),
        "topic": facts.get("topic"),
        "family": facts.get("family_label"),
        "delivery": event_snapshot.get("delivery"),
        "location": place,
        "host": (facts.get("people") or {}).get("host"),
        "summary": (content or {}).get("short_summary"),
        "event_states": sorted(_states_in_text(place or "")),
    }


# --- Deterministic filtering and signals -----------------------------------------------------


def hard_filter(record: FacebookGroupRecord, context: dict) -> str | None:
    """Return a machine-readable exclusion reason, or None when the Group may be considered."""
    if record.import_status != "CURRENT":
        return "SOURCE_SUPERSEDED"
    if record.identity_status == "UNRESOLVED_SHARE_ALIAS":
        return "UNRESOLVED_IDENTITY"
    if record.identity_status != "GROUP_PATH" or not record.chosen_url:
        return "UNRECOGNIZED_URL"
    if record.review_status == "CONFLICT":
        return "IDENTITY_CONFLICT_NEEDS_REVIEW"
    if record.review_status == "EXCLUDED":
        flags = {flag["code"] for flag in record.quality_flags}
        return "DUPLICATE_SOURCE_ROW" if "DUPLICATE_SOURCE_ROW" in flags else "CATALOG_EXCLUDED"
    if any(item.get("blocks_event_promotion") for item in record.restrictions):
        return "EVENT_PROMOTION_RESTRICTED"
    geography = record.geography
    event_states = set(context["event_states"])
    if (
        context["delivery"] == "IN_PERSON"
        and event_states
        and geography.get("scope") == "STATES"
        and geography.get("status") in {"RECOGNIZED", "PARTIAL"}
        and not event_states & set(geography.get("codes", []))
    ):
        return "GEOGRAPHY_MISMATCH"
    return None


def signals_for(record: FacebookGroupRecord, context: dict) -> tuple[list[str], list[str], int]:
    geography = record.geography
    signals: list[str] = []
    cautions: list[str] = []
    score = 0
    codes = set(geography.get("codes", []))
    reliable = geography.get("status") in {"RECOGNIZED", "PARTIAL"}
    if context["delivery"] == "VIRTUAL":
        signals.append("VIRTUAL_EVENT_ANY_LOCATION")
        score += 1
    elif reliable and codes & set(context["event_states"]):
        signals.append("LOCAL_GEOGRAPHY")
        score += 4
    elif geography.get("scope") == "NATIONAL":
        signals.append("NATIONAL_AUDIENCE")
        score += 1
    if not reliable or geography.get("scope") == "UNKNOWN":
        cautions.append("GEOGRAPHY_UNCERTAIN")
        score -= 1

    category = ((record.audience or {}).get("category_clue") or "").casefold()
    name_words = set(tokens(record.display_name))
    if "business" in name_words or category in {"event marketing", "business"}:
        signals.append("BUSINESS_COMMUNITY")
        score += 2
    topic_words = (
        set(tokens(context.get("topic"), context.get("family"), context.get("title")))
        - GENERIC_TOPIC_WORDS
        - {name.casefold() for name in US_STATES.values()}
        - {code.casefold() for code in US_STATES}
    )
    if topic_words & (set(record.search_tokens) - GENERIC_TOPIC_WORDS):
        signals.append("TOPIC_MATCH")
        score += 2
    if category in OFF_AUDIENCE_CATEGORIES or name_words & OFF_AUDIENCE_WORDS:
        cautions.append("AUDIENCE_MISMATCH")
        score -= 3

    scopes = {item.get("scope") for item in record.restrictions}
    if "MEMBERSHIP_OR_POSTING_RULE" in scopes:
        cautions.append("POSTING_RULES")
    if "GROUP_ADMINISTRATION" in scopes:
        cautions.append("ADMIN_CHANGE_RESTRICTION")
    if any(flag["code"] == "OWNERSHIP_NOTE_NOT_MY_GROUP" for flag in record.quality_flags):
        cautions.append("OWNERSHIP_NOTE")
    return signals, cautions, score


def tier_for(score: int) -> str | None:
    if score >= 6:
        return "BEST_FIT"
    if score >= 3:
        return "GOOD_FIT"
    if score >= 1:
        return "POSSIBLE_FIT"
    return None


def group_view(record: FacebookGroupRecord, context: dict | None = None) -> dict:
    exclusion = hard_filter(record, context) if context else None
    caution_codes = signals_for(record, context)[1] if context else []
    return {
        "id": str(record.id),
        "display_name": record.display_name,
        "url": record.chosen_url,
        "geography_label": record.geography.get("label"),
        "member_count": record.member_count,
        "category": (record.audience or {}).get("category_clue"),
        "cautions": [CAUTION_TEXT[code] for code in caution_codes],
        "selectable": exclusion is None,
        "unavailable_reason": EXCLUSION_TEXT.get(exclusion) if exclusion else None,
    }


def _candidate(record: FacebookGroupRecord, context: dict) -> dict:
    signals, cautions, score = signals_for(record, context)
    return {
        "group_id": str(record.id),
        "name": record.display_name,
        "geography": record.geography.get("label"),
        "state_codes": record.geography.get("codes", []),
        "member_count": record.member_count,
        "category": (record.audience or {}).get("category_clue"),
        "signals": signals,
        "cautions": [CAUTION_TEXT[code] for code in cautions],
        "score": score,
        "tier": tier_for(score),
    }


def _sorted_candidates(candidates: list[dict]) -> list[dict]:
    return sorted(candidates, key=lambda c: (-c["score"], c["name"].casefold(), c["group_id"]))


# --- Loading helpers -------------------------------------------------------------------------


def _find_campaign(db: Session, campaign_id, *, lock: bool = False) -> tuple[Campaign, Event]:
    try:
        parsed = uuid.UUID(str(campaign_id))
    except ValueError:
        raise HTTPException(404, "CAMPAIGN_NOT_FOUND") from None
    campaign = db.get(Campaign, parsed)
    if campaign is None:
        raise HTTPException(404, "CAMPAIGN_NOT_FOUND")
    query = select(Event).where(Event.id == campaign.event_id)
    event = db.scalar(query.with_for_update() if lock else query)
    if lock:
        campaign = db.scalar(select(Campaign).where(Campaign.id == parsed).with_for_update())
    return campaign, event


def _current_version(db: Session, campaign: Campaign) -> CampaignVersion:
    if campaign.current_version_id is None:
        raise HTTPException(409, "CAMPAIGN_VERSION_NOT_FOUND")
    return db.get(CampaignVersion, campaign.current_version_id)


def _facebook_enabled(version: CampaignVersion) -> bool:
    return "FACEBOOK_GROUPS" in (version.snapshot.get("selected_surfaces") or [])


def _context_for(event: Event, version: CampaignVersion) -> dict:
    from app.campaign_service import material_event_snapshot

    return event_context(material_event_snapshot(event), version.snapshot.get("content"))


def latest_selected_ids(db: Session, campaign_id) -> list[uuid.UUID]:
    """Highest campaign revision per Group wins; timestamps never break ties."""
    latest = (
        select(
            GroupDecision.group_record_id,
            func.max(GroupDecision.campaign_revision).label("revision"),
        )
        .where(GroupDecision.campaign_id == campaign_id)
        .group_by(GroupDecision.group_record_id)
        .subquery()
    )
    rows = db.execute(
        select(GroupDecision.group_record_id)
        .join(
            latest,
            (GroupDecision.group_record_id == latest.c.group_record_id)
            & (GroupDecision.campaign_revision == latest.c.revision),
        )
        .where(GroupDecision.campaign_id == campaign_id, GroupDecision.decision == "SELECT")
    ).all()
    return sorted((row[0] for row in rows), key=str)


def version_group_ids(version: CampaignVersion) -> list[str]:
    return sorted(item["group_record_id"] for item in version.snapshot.get(SNAPSHOT_KEY, []))


def selection_pending(db: Session, campaign: Campaign, version: CampaignVersion) -> bool:
    if not _facebook_enabled(version):
        return False
    return [str(value) for value in latest_selected_ids(db, campaign.id)] != version_group_ids(
        version
    )


# --- Snapshot assembly (called while creating every successor version) ----------------------


def assemble_group_targets(
    db: Session, campaign: Campaign, event_snapshot: dict, content: dict, surfaces: list[str]
) -> list[dict]:
    """Freeze the operator's latest selections, revalidated against the catalog and this Event."""
    if "FACEBOOK_GROUPS" not in surfaces:
        return []
    selected = latest_selected_ids(db, campaign.id)
    if not selected:
        return []
    context = event_context(event_snapshot, content)
    records = db.scalars(
        select(FacebookGroupRecord).where(FacebookGroupRecord.id.in_(selected))
    ).all()
    if len(records) != len(selected):
        raise HTTPException(409, "GROUP_SELECTION_INVALID")
    decisions = {
        row.group_record_id: row
        for row in db.scalars(
            select(GroupDecision)
            .where(
                GroupDecision.campaign_id == campaign.id,
                GroupDecision.group_record_id.in_(selected),
            )
            .order_by(GroupDecision.campaign_revision)
        ).all()
    }
    targets = []
    for record in sorted(records, key=lambda r: str(r.id)):
        if hard_filter(record, context) is not None:
            raise HTTPException(409, "GROUP_SELECTION_INVALID")
        view = group_view(record, context)
        targets.append(
            {
                "group_record_id": str(record.id),
                "group_revision": record.revision,
                "display_name": record.display_name,
                "chosen_url": record.chosen_url,
                "geography_label": view["geography_label"],
                "member_count": record.member_count,
                "cautions": view["cautions"],
                "decision_id": str(decisions[record.id].id),
            }
        )
    return targets


def hash_projection(targets: list[dict]) -> list[dict]:
    """Approval-significant identity only; explanations/ranking are not hashed."""
    return [
        {
            "group_record_id": item["group_record_id"],
            "display_name": item["display_name"],
            "chosen_url": item["chosen_url"],
        }
        for item in targets
    ]


def assert_targets_available(db: Session, targets: list[dict]) -> None:
    """A catalog change after approval blocks delivery; it never redirects to another Group."""
    if not targets:
        return
    ids = [uuid.UUID(item["group_record_id"]) for item in targets]
    records = {
        record.id: record
        for record in db.scalars(
            select(FacebookGroupRecord).where(FacebookGroupRecord.id.in_(ids))
        ).all()
    }
    for item in targets:
        record = records.get(uuid.UUID(item["group_record_id"]))
        if (
            record is None
            or record.import_status != "CURRENT"
            or record.review_status in {"CONFLICT", "EXCLUDED"}
            or record.chosen_url != item["chosen_url"]
        ):
            raise HTTPException(409, "GROUP_TARGET_UNAVAILABLE")


# --- Recommendation runs ---------------------------------------------------------------------


def generate_recommendations(sessions: sessionmaker, campaign_id, ranker: GroupRanker) -> dict:
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        version = _current_version(db, campaign)
        if not _facebook_enabled(version):
            raise HTTPException(409, "FACEBOOK_GROUPS_NOT_SELECTED")
        context = _context_for(event, version)
        records = db.scalars(select(FacebookGroupRecord)).all()
        if not records:
            raise HTTPException(409, "GROUP_CATALOG_EMPTY")
        evaluations = []
        for record in records:
            exclusion = hard_filter(record, context)
            evaluations.append(
                (record, exclusion, None if exclusion else _candidate(record, context))
            )
        event_revision = event.revision
        captured_campaign_id = campaign.id

    candidates = _sorted_candidates([item for _, _, item in evaluations if item])
    shortlistable = [c for c in candidates if c["tier"]][:MAX_MODEL_CANDIDATES]
    payload = model_payload(context, shortlistable)
    metadata = {
        "ranker_id": getattr(ranker, "ranker_id", "unknown"),
        "prompt_version": PROMPT_VERSION,
        "candidate_count": len(candidates),
        "sent_to_ranker": len(shortlistable),
        "run_notes": [],
    }
    if context["delivery"] == "IN_PERSON" and not context["event_states"]:
        metadata["run_notes"].append("EVENT_STATE_UNKNOWN")
    if shortlistable:
        try:
            result = ranker.rank(context, shortlistable)
            ranked = result.ranked
            metadata.update(result.metadata)
            metadata["ranking_source"] = (
                "MODEL" if result.metadata.get("model_id") else "DETERMINISTIC"
            )
        except RankingFailed as failure:
            LOGGER.warning("group_ranking_fallback category=%s", failure.category)
            ranked = deterministic_ranking(shortlistable, context)
            metadata.update(ranking_source="DETERMINISTIC_FALLBACK", failure=failure.category)
        except Exception:
            LOGGER.warning("group_ranking_fallback category=UNEXPECTED")
            ranked = deterministic_ranking(shortlistable, context)
            metadata.update(ranking_source="DETERMINISTIC_FALLBACK", failure="UNEXPECTED")
    else:
        ranked = []
        metadata["ranking_source"] = "DETERMINISTIC"
    ranked = sorted(
        ranked[:MAX_SHORTLIST], key=lambda item: TIER_ORDER[item.tier]
    )  # stable: keeps ranker order within a tier
    input_hash = _sha({"payload": payload, "ranker": metadata["ranker_id"]})
    by_group = {item.group_id: (rank, item) for rank, item in enumerate(ranked, start=1)}
    candidate_by_id = {c["group_id"]: c for c in candidates}

    run_id = uuid.uuid4()
    with sessions.begin() as db:
        campaign, event = _find_campaign(db, captured_campaign_id, lock=True)
        if event.revision != event_revision:
            raise HTTPException(409, "CAMPAIGN_INPUTS_CHANGED")
        at = _now()
        for record, exclusion, _ in evaluations:
            key = str(record.id)
            ranked_item = by_group.get(key)
            candidate = candidate_by_id.get(key)
            db.add(
                GroupRecommendation(
                    id=uuid.uuid4(),
                    campaign_id=campaign.id,
                    group_record_id=record.id,
                    run_id=run_id,
                    event_revision=event_revision,
                    group_revision=record.revision,
                    filter_result=f"EXCLUDED:{exclusion}" if exclusion else "CANDIDATE",
                    rank=ranked_item[0] if ranked_item else None,
                    tier=ranked_item[1].tier if ranked_item else None,
                    reason=ranked_item[1].reason[:400] if ranked_item else None,
                    signals=ranked_item[1].signals
                    if ranked_item
                    else (candidate["signals"] if candidate else []),
                    cautions=(
                        [ranked_item[1].caution] if ranked_item and ranked_item[1].caution else []
                    ),
                    score=candidate["score"] if candidate else None,
                    explanation_source=ranked_item[1].explanation_source if ranked_item else None,
                    input_hash=input_hash,
                    model_metadata=copy.deepcopy(metadata),
                    created_at=at,
                )
            )
        db.flush()
    return recommendations_view(sessions, captured_campaign_id)


def _latest_run(db: Session, campaign_id) -> uuid.UUID | None:
    return db.scalar(
        select(GroupRecommendation.run_id)
        .where(GroupRecommendation.campaign_id == campaign_id)
        .order_by(GroupRecommendation.created_at.desc(), GroupRecommendation.run_id)
        .limit(1)
    )


def recommendations_view(sessions: sessionmaker, campaign_id) -> dict:
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        version = _current_version(db, campaign)
        enabled = _facebook_enabled(version)
        context = _context_for(event, version)
        selected_ids = latest_selected_ids(db, campaign.id)
        selected_records = (
            db.scalars(
                select(FacebookGroupRecord).where(FacebookGroupRecord.id.in_(selected_ids))
            ).all()
            if selected_ids
            else []
        )
        run_id = _latest_run(db, campaign.id)
        run = None
        recommendations: list[dict] = []
        if run_id is not None:
            rows = db.execute(
                select(GroupRecommendation, FacebookGroupRecord)
                .join(
                    FacebookGroupRecord,
                    FacebookGroupRecord.id == GroupRecommendation.group_record_id,
                )
                .where(GroupRecommendation.run_id == run_id)
            ).all()
            first = rows[0][0]
            excluded: dict[str, int] = {}
            stale = first.event_revision != event.revision
            for recommendation, record in rows:
                if recommendation.filter_result.startswith("EXCLUDED:"):
                    code = recommendation.filter_result.split(":", 1)[1]
                    excluded[code] = excluded.get(code, 0) + 1
                if recommendation.group_revision != record.revision:
                    stale = True
                if recommendation.rank is not None:
                    recommendations.append(
                        {
                            "recommendation_id": str(recommendation.id),
                            "rank": recommendation.rank,
                            "tier": recommendation.tier,
                            "reason": recommendation.reason,
                            "signals": recommendation.signals,
                            "caution": recommendation.cautions[0]
                            if recommendation.cautions
                            else None,
                            "explanation_source": recommendation.explanation_source,
                            "group": group_view(record, context),
                        }
                    )
            recommendations.sort(key=lambda item: item["rank"])
            meta = first.model_metadata
            run = {
                "run_id": str(run_id),
                "created_at": first.created_at.isoformat(),
                "ranking_source": meta.get("ranking_source"),
                "candidate_count": meta.get("candidate_count", 0),
                "excluded_counts": excluded,
                "run_notes": meta.get("run_notes", []),
                "stale": stale,
            }
        return {
            "campaign_id": str(campaign.id),
            "campaign_revision": campaign.revision,
            "facebook_groups_enabled": enabled,
            "run": run,
            "recommendations": recommendations,
            "selection": {
                "selected": [
                    group_view(record, context)
                    for record in sorted(selected_records, key=lambda r: r.display_name.casefold())
                ],
                "pending": selection_pending(db, campaign, version) if enabled else False,
                "saved_group_ids": version_group_ids(version),
                "version_number": version.version_number,
                "approval_active": campaign.active_approval_id is not None,
            },
        }


def eligible_groups(sessions: sessionmaker, campaign_id, query: str | None, limit: int) -> dict:
    """Manual browsing of every eligible Group; also the fallback when ranking is unavailable."""
    words = set(tokens(query)) if query else set()
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        version = _current_version(db, campaign)
        if not _facebook_enabled(version):
            raise HTTPException(409, "FACEBOOK_GROUPS_NOT_SELECTED")
        context = _context_for(event, version)
        records = db.scalars(select(FacebookGroupRecord)).all()
        candidates = []
        total = 0
        for record in records:
            if hard_filter(record, context) is not None:
                continue
            total += 1
            if words and not words <= set(record.search_tokens):
                continue
            candidate = _candidate(record, context)
            candidates.append((candidate, record))
        candidates.sort(key=lambda pair: (-pair[0]["score"], pair[1].display_name.casefold()))
        return {
            "eligible_count": total,
            "groups": [
                {**group_view(record, context), "tier": candidate["tier"]}
                for candidate, record in candidates[:limit]
            ],
        }


# --- Human decisions and applying a selection ------------------------------------------------


def record_decision(
    sessions: sessionmaker,
    campaign_id,
    actor_id,
    *,
    expected_revision: int,
    group_record_id: str,
    decision: str,
    recommendation_id: str | None,
    confirm_withdraw_approval: bool,
) -> dict:
    try:
        group_id = uuid.UUID(group_record_id)
        recommendation_uuid = uuid.UUID(recommendation_id) if recommendation_id else None
    except ValueError:
        raise HTTPException(422, "INVALID_REQUEST") from None
    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        if campaign.revision != expected_revision:
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        version = _current_version(db, campaign)
        if not _facebook_enabled(version):
            raise HTTPException(409, "FACEBOOK_GROUPS_NOT_SELECTED")
        record = db.get(FacebookGroupRecord, group_id)
        if record is None:
            raise HTTPException(404, "GROUP_NOT_FOUND")
        if recommendation_uuid is not None:
            recommendation = db.get(GroupRecommendation, recommendation_uuid)
            if (
                recommendation is None
                or recommendation.campaign_id != campaign.id
                or recommendation.group_record_id != record.id
            ):
                raise HTTPException(422, "RECOMMENDATION_NOT_APPLICABLE")
        currently_selected = record.id in set(latest_selected_ids(db, campaign.id))
        if (decision == "SELECT") == currently_selected:
            return {"changed": False, "campaign_revision": campaign.revision}
        if decision == "SELECT":
            context = _context_for(event, version)
            if hard_filter(record, context) is not None:
                raise HTTPException(409, "GROUP_NOT_ELIGIBLE")
        if campaign.active_approval_id is not None:
            if not confirm_withdraw_approval:
                raise HTTPException(409, "GROUP_CHANGE_REQUIRES_NEW_APPROVAL")
            from app.execution_service import prepare_for_successor

            # The approved plan stays exactly as approved; only its active authority ends.
            prepare_for_successor(db, campaign, event, actor_id)
            campaign.state = "REVIEW"
        campaign.revision += 1
        campaign.updated_at = _now()
        row = GroupDecision(
            id=uuid.uuid4(),
            campaign_id=campaign.id,
            group_record_id=record.id,
            recommendation_id=recommendation_uuid,
            actor_id=actor_id,
            decision=decision,
            campaign_revision=campaign.revision,
            event_revision=event.revision,
            group_revision=record.revision,
            reason=None,
            created_at=_now(),
        )
        db.add(row)
        from app.execution_service import _audit

        _audit(
            db,
            "GROUP_DECISION_RECORDED",
            actor_id=actor_id,
            event_id=event.id,
            campaign_id=campaign.id,
            metadata={"decision": decision, "group_record_id": str(record.id)},
        )
        db.flush()
        return {"changed": True, "campaign_revision": campaign.revision}


def apply_selection(sessions: sessionmaker, campaign_id, actor_id, expected_revision: int) -> dict:
    """Create a successor version whose snapshot freezes the current Group selection."""
    from app.campaign_service import (
        CampaignContent,
        _create_version,
        campaign_view,
        is_stale,
        material_event_snapshot,
        validate_surfaces,
    )
    from app.execution_service import prepare_for_successor

    with sessions.begin() as db:
        campaign, event = _find_campaign(db, campaign_id, lock=True)
        if campaign.revision != expected_revision:
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        version = _current_version(db, campaign)
        if not _facebook_enabled(version):
            raise HTTPException(409, "FACEBOOK_GROUPS_NOT_SELECTED")
        if is_stale(event, version):
            raise HTTPException(409, "CAMPAIGN_STALE_REGENERATE_FIRST")
        if not selection_pending(db, campaign, version):
            raise HTTPException(409, "GROUP_SELECTION_UNCHANGED")
        prepare_for_successor(db, campaign, event, actor_id)
        count = len(latest_selected_ids(db, campaign.id))
        _create_version(
            db,
            campaign,
            material_event_snapshot(event),
            CampaignContent.model_validate(version.snapshot["content"], strict=True),
            validate_surfaces(version.snapshot["selected_surfaces"]),
            actor_id,
            revision_request=(
                f"Facebook Groups updated: {count} selected."
                if count
                else "Facebook Groups updated: none selected."
            ),
            revision_targets=[REVISION_TARGET],
        )
        db.flush()
        return campaign_view(db, campaign, event)
