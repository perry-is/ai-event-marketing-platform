import hashlib
import json
import uuid

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.group_catalog import (
    CatalogImportError,
    group_rows,
    import_group_master,
    normalize_row,
    read_workbook,
)
from app.group_ranker import (
    DeterministicGroupRanker,
    OpenAIGroupRanker,
    RankingFailed,
    RankingResult,
)
from app.group_recommendations import event_context, hard_filter, signals_for
from app.models import (
    Approval,
    Campaign,
    CampaignVersion,
    FacebookGroupRecord,
    GroupDecision,
    GroupRecommendation,
)
from tests.conftest import sign_in
from tests.openai_test_support import mock_response
from tests.sample_group_master import (
    SAMPLE_MASTER_NAME,
    headed,
    sample_group_master,
    workbook,
)
from tests.test_execution import _campaign, _executions, _headers, _ready_event, _start


def _import_master(app, settings):
    return import_group_master(
        app.state.database.sessions,
        settings.private_storage_dir,
        sample_group_master(),
        SAMPLE_MASTER_NAME,
    )


SYNTHETIC = {
    "Team A": (
        headed(
            ["Denver Business Network", "CO", "https://www.facebook.com/groups/denverbc", 900],
            [
                "Aurora Business Network",
                "CO",
                "https://www.facebook.com/groups/aurorabc",
                400,
            ],
            ["Texas Business Network", "TX", "https://www.facebook.com/groups/txbc", 5000],
            ["Share Link Group", "CO", "https://www.facebook.com/share/g/abc123/?x=1", 50],
            ["Section heading", None, None, None],
        ),
        {},
    )
}


def _records(app):
    with app.state.database.sessions() as db:
        return db.scalars(
            select(FacebookGroupRecord).order_by(FacebookGroupRecord.source_row)
        ).all()


# --- Import ----------------------------------------------------------------------------------


def test_group_master_imports_with_provenance_and_original_unchanged(app, settings):
    data = sample_group_master()
    before = hashlib.sha256(data).hexdigest()
    result = _import_master(app, settings)
    assert hashlib.sha256(data).hexdigest() == before
    assert result.group_rows == 32 and result.created == 32 and not result.already_imported
    records = _records(app)
    assert {r.source_tab for r in records} == {"Team A", "Team B", "Team C", "Available"}
    assert sum(r.identity_status == "UNRESOLVED_SHARE_ALIAS" for r in records) == 3
    assert (
        len(
            {
                r.raw_record["cells"]["C" if r.source_tab != "Available" else "B"]["value"]
                for r in records
            }
        )
        == 30
    )
    by_coord = {(r.source_tab, r.source_row): r for r in records}
    # Same URL with conflicting name/geography is never merged.
    assert by_coord[("Team B", 4)].review_status == "CONFLICT"
    assert by_coord[("Team B", 5)].review_status == "CONFLICT"
    # Hyperlink disagreement is preserved and flagged, not silently resolved.
    parker = by_coord[("Team A", 15)]
    assert {u["origin"] for u in parker.recorded_urls} == {"LINK_COLUMN", "NAME_HYPERLINK"}
    assert parker.review_status == "CONFLICT"
    # Same name, different numeric group IDs: kept separate and needing review.
    omaha = [r for r in records if r.display_name == "Omaha Business Network"]
    assert len(omaha) == 2 and {r.review_status for r in omaha} == {"CONFLICT"}
    # An exact duplicate row is excluded rather than double-counted.
    assert by_coord[("Team C", 5)].review_status == "EXCLUDED"
    # Exact restriction text survives with its cell provenance.
    restricted = by_coord[("Team A", 16)]
    assert restricted.restrictions[0]["scope"] == "GROUP_ADMINISTRATION"
    assert restricted.restrictions[0]["cell"] == "A16"
    assert "DO NOT MAKE ANY CHANGES" in restricted.raw_record["cells"]["A"]["value"]
    assert by_coord[("Team A", 17)].restrictions[0]["scope"] == "EVENT_PROMOTION"
    # Contradictory geography is flagged rather than corrected.
    assert by_coord[("Team C", 3)].geography["status"] == "CONFLICTING"
    with app.state.database.sessions() as db:
        source = db.execute(
            text("SELECT catalog_kind, event_id, sha256 FROM source_materials")
        ).one()
    assert source.catalog_kind == "FACEBOOK_GROUP_MASTER" and source.event_id is None
    assert source.sha256 == before


def test_repeat_import_is_idempotent(app, settings):
    first = _import_master(app, settings)
    second = _import_master(app, settings)
    assert second.already_imported and second.created == 0
    assert second.source_material_id == first.source_material_id
    assert len(_records(app)) == 32


def test_changed_source_retains_unchanged_rows_and_never_overwrites_curation(app, settings):
    sessions = app.state.database.sessions
    import_group_master(sessions, settings.private_storage_dir, workbook(SYNTHETIC), "v1.xlsx")
    records = _records(app)
    assert len(records) == 4
    denver = next(r for r in records if r.display_name == "Denver Business Network")
    with sessions.begin() as db:
        row = db.get(FacebookGroupRecord, denver.id)
        row.review_status = "USABLE"
    changed = {
        "Team A": (
            headed(
                ["Denver Business Network", "CO", "https://www.facebook.com/groups/denverbc", 900],
                [
                    "Aurora Business Network",
                    "CO",
                    "https://www.facebook.com/groups/aurorabc",
                    450,
                ],
                ["Utah Business Network", "UT", "https://www.facebook.com/groups/utbc", 70],
            ),
            {},
        )
    }
    result = import_group_master(
        sessions, settings.private_storage_dir, workbook(changed), "v2.xlsx"
    )
    assert (result.created, result.retained, result.superseded) == (2, 1, 3)
    after = {r.id: r for r in _records(app)}
    assert after[denver.id].review_status == "USABLE"
    assert after[denver.id].import_status == "CURRENT"
    old_aurora = [r for r in after.values() if r.display_name.startswith("Aurora")]
    assert {r.import_status for r in old_aurora} == {"CURRENT", "SUPERSEDED"}


def test_malformed_and_ambiguous_rows_are_handled_safely(app, settings):
    with pytest.raises(CatalogImportError):
        import_group_master(
            app.state.database.sessions, settings.private_storage_dir, b"not a workbook", "bad.xlsx"
        )
    data = workbook({**SYNTHETIC, "Notes": ({1: {"A": "Random"}}, {})})
    rows, skipped = group_rows(read_workbook(data))
    assert len(rows) == 4
    assert {"tab": "Notes", "reason": "UNRECOGNIZED_TAB_LAYOUT"} in skipped
    assert any(item.get("reason") == "NOT_A_GROUP_ROW" for item in skipped)
    alias = normalize_row(next(r for r in rows if r.value("A") == "Share Link Group"))
    assert alias["identity_status"] == "UNRESOLVED_SHARE_ALIAS"
    assert alias["chosen_url"] is None and alias["group_key"] is None
    import_group_master(app.state.database.sessions, settings.private_storage_dir, data, "x.xlsx")
    record = _records(app)[0]
    with pytest.raises(DBAPIError):
        with app.state.database.sessions.begin() as db:
            db.execute(
                text("UPDATE facebook_group_records SET raw_record = '{}'::jsonb WHERE id = :id"),
                {"id": record.id},
            )


# --- Filtering -------------------------------------------------------------------------------


def _record(**changes):
    rows, _ = group_rows(read_workbook(workbook(SYNTHETIC)))
    base = normalize_row(rows[0])
    base.update(changes)
    return FacebookGroupRecord(
        id=uuid.uuid4(),
        review_status=changes.pop("review_status", "UNREVIEWED"),
        import_status=changes.pop("import_status", "CURRENT"),
        revision=1,
        **{k: v for k, v in base.items() if k not in {"review_status", "import_status"}},
    )


def _context(delivery="IN_PERSON", venue="Aurora Event Center, Aurora, CO", topic=None):
    return event_context(
        {
            "title": "Lunch & Learn",
            "delivery": delivery,
            "facts": {"location": {"venue": venue}, "topic": topic},
        },
        {"short_summary": "An Event Marketing gathering."},
    )


def test_hard_filters_have_machine_readable_reasons():
    context = _context()
    assert context["event_states"] == ["CO"]
    assert hard_filter(_record(), context) is None
    assert (
        hard_filter(_record(identity_status="UNRESOLVED_SHARE_ALIAS", chosen_url=None), context)
        == "UNRESOLVED_IDENTITY"
    )
    assert hard_filter(_record(review_status="CONFLICT"), context) == (
        "IDENTITY_CONFLICT_NEEDS_REVIEW"
    )
    assert hard_filter(_record(import_status="SUPERSEDED"), context) == "SOURCE_SUPERSEDED"
    blocked = _record(restrictions=[{"scope": "EVENT_PROMOTION", "blocks_event_promotion": True}])
    assert hard_filter(blocked, context) == "EVENT_PROMOTION_RESTRICTED"
    texas = _record(geography={"codes": ["TX"], "scope": "STATES", "status": "RECOGNIZED"})
    assert hard_filter(texas, context) == "GEOGRAPHY_MISMATCH"
    # Virtual events are not restricted by group geography; unknown geography is not excluded.
    assert hard_filter(texas, _context(delivery="VIRTUAL")) is None
    unknown = _record(geography={"codes": [], "scope": "UNKNOWN", "status": "UNRECOGNIZED"})
    assert hard_filter(unknown, context) is None
    assert "GEOGRAPHY_UNCERTAIN" in signals_for(unknown, context)[1]
    # Without a known Event state, geography is not used to exclude anything.
    assert hard_filter(texas, _context(venue="Example Venue")) is None


def test_signals_do_not_treat_size_as_relevance():
    context = _context(topic="Real estate marketing")
    small = _record(
        member_count=10,
        display_name="Real Estate Business Network",
        search_tokens=["real", "estate", "business", "connect"],
    )
    large = _record(
        member_count=60000,
        display_name="Garage Sale Swap",
        search_tokens=["garage", "sale", "swap"],
        geography={"codes": ["CO"], "scope": "STATES", "status": "RECOGNIZED"},
    )
    small_signals, _, small_score = signals_for(small, context)
    large_signals, large_cautions, large_score = signals_for(large, context)
    assert {"LOCAL_GEOGRAPHY", "BUSINESS_COMMUNITY", "TOPIC_MATCH"} <= set(small_signals)
    assert "AUDIENCE_MISMATCH" in large_cautions
    assert small_score > large_score


# --- Recommendation API, ranking, selection and approval -----------------------------------


@pytest.fixture
def groups_app(app, settings):
    _import_master(app, settings)
    return app


def _setup(client, settings, provider, venue="Aurora Event Center, Aurora, CO"):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    response = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": event["revision"],
            "changes": [{"field_path": "facts.location.venue", "value": venue}],
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return _campaign(client, settings, response.json())


def _recommend(client, settings, campaign):
    response = client.post(
        f"/api/campaigns/{campaign['id']}/group-recommendations",
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _decide(client, settings, campaign_id, group_id, decision="SELECT", **extra):
    revision = client.get(f"/api/campaigns/{campaign_id}").json()["revision"]
    return client.post(
        f"/api/campaigns/{campaign_id}/group-decisions",
        json={
            "expected_revision": revision,
            "group_record_id": group_id,
            "decision": decision,
            **extra,
        },
        headers=_headers(client, settings),
    )


def _save(client, settings, campaign_id):
    revision = client.get(f"/api/campaigns/{campaign_id}").json()["revision"]
    response = client.post(
        f"/api/campaigns/{campaign_id}/group-selection",
        json={"expected_revision": revision},
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approve_current(client, settings, campaign_id, replacements=()):
    preview = client.get(f"/api/campaigns/{campaign_id}/approval-preview").json()
    assert preview["can_approve"], preview
    response = client.post(
        f"/api/campaigns/{campaign_id}/approve",
        json={
            "expected_revision": preview["campaign"]["revision"],
            "campaign_version_id": preview["version_id"],
            "version_hash": preview["version_hash"],
            "expected_destination_plan_hash": preview["destination_plan_hash"],
            "replacement_acknowledgements": list(replacements),
            "uncertain_risk_acknowledgements": [],
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_group_apis_require_authentication_and_csrf(groups_app, client, settings, provider):
    identifier = str(uuid.uuid4())
    for method, path in (
        ("get", f"/api/campaigns/{identifier}/group-recommendations"),
        ("post", f"/api/campaigns/{identifier}/group-recommendations"),
        ("get", f"/api/campaigns/{identifier}/eligible-groups"),
        ("post", f"/api/campaigns/{identifier}/group-decisions"),
        ("post", f"/api/campaigns/{identifier}/group-selection"),
    ):
        assert getattr(client, method)(path).status_code == 401
    campaign = _setup(client, settings, provider)
    response = client.post(
        f"/api/campaigns/{campaign['id']}/group-recommendations",
        headers={"origin": settings.app_origin},
    )
    assert response.status_code == 403


def test_recommendations_are_bounded_explained_and_persist_exclusions(
    groups_app, client, settings, provider
):
    seen: list[list[dict]] = []

    class CapturingRanker(DeterministicGroupRanker):
        def rank(self, context, candidates):
            seen.append(candidates)
            return super().rank(context, candidates)

    groups_app.state.group_ranker = CapturingRanker()
    campaign = _setup(client, settings, provider)
    view = _recommend(client, settings, campaign)
    assert 5 <= len(view["recommendations"]) <= 12
    assert view["run"]["ranking_source"] == "DETERMINISTIC"
    assert len(seen[0]) <= 30
    with groups_app.state.database.sessions() as db:
        excluded = {
            row.group_record_id: row.filter_result
            for row in db.scalars(select(GroupRecommendation)).all()
            if row.filter_result.startswith("EXCLUDED:")
        }
        aliases = db.scalars(
            select(FacebookGroupRecord.id).where(
                FacebookGroupRecord.identity_status == "UNRESOLVED_SHARE_ALIAS"
            )
        ).all()
    candidate_ids = {c["group_id"] for c in seen[0]}
    assert not candidate_ids & {str(value) for value in excluded}
    assert all(excluded[alias] == "EXCLUDED:UNRESOLVED_IDENTITY" for alias in aliases)
    assert view["run"]["excluded_counts"]["GEOGRAPHY_MISMATCH"] > 0
    first = view["recommendations"][0]
    assert first["tier"] in {"BEST_FIT", "GOOD_FIT", "POSSIBLE_FIT"}
    assert first["reason"] and "0." not in first["reason"]
    assert first["group"]["url"].startswith("https://www.facebook.com/groups/")
    assert "Colorado" in (first["group"]["geography_label"] or "")
    assert view["selection"]["selected"] == []  # Recommendations never select anything.


def _openai_ranker(handler):
    return OpenAIGroupRanker(
        "test-provider-key",
        "gpt-6-luna",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


def test_openai_ranker_uses_strict_bounded_request_and_validates_ids(
    groups_app, client, settings, provider
):
    requests = []
    behaviour = {"mode": "valid"}

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        candidates = json.loads(body["input"][0]["content"])["candidates"]
        first, second = candidates[0]["group_id"], candidates[1]["group_id"]
        items = [
            {
                "group_id": first,
                "tier": "BEST_FIT",
                "reason": "Colorado business community.",
                "matching_signals": ["LOCAL_GEOGRAPHY", "TOPIC_MATCH"],
                "caution": None,
            },
            {
                "group_id": first,
                "tier": "GOOD_FIT",
                "reason": "Duplicate entry.",
                "matching_signals": [],
                "caution": None,
            },
            {
                "group_id": second,
                "tier": "GOOD_FIT",
                "reason": "Has 999999 Texas members.",
                "matching_signals": ["BUSINESS_COMMUNITY"],
                "caution": "Safe for automation",
            },
        ]
        if behaviour["mode"] == "unknown":
            items.append(
                {
                    "group_id": str(uuid.uuid4()),
                    "tier": "BEST_FIT",
                    "reason": "Made up.",
                    "matching_signals": [],
                    "caution": None,
                }
            )
        return mock_response({"recommendations": items})

    groups_app.state.group_ranker = _openai_ranker(handler)
    campaign = _setup(client, settings, provider)
    view = _recommend(client, settings, campaign)
    body = requests[0]
    assert body["text"]["format"]["strict"] is True and body["store"] is False
    assert body["tools"] == []
    payload = json.loads(body["input"][0]["content"])
    assert set(payload) == {"event", "candidates"}
    assert len(payload["candidates"]) <= 30
    serialized = json.dumps(payload)
    assert "facebook.com" not in serialized and "Admin" not in serialized
    assert view["run"]["ranking_source"] == "MODEL"
    recs = view["recommendations"]
    assert len(recs) == 2  # duplicate normalized to first occurrence
    assert "TOPIC_MATCH" not in recs[0]["signals"]  # not a computed signal for this candidate
    # Unsupported facts in a model explanation are replaced, and invented cautions dropped.
    assert recs[1]["explanation_source"] == "DETERMINISTIC"
    assert "999999" not in recs[1]["reason"] and recs[1]["caution"] != "Safe for automation"

    behaviour["mode"] = "unknown"
    fallback = _recommend(client, settings, campaign)
    assert fallback["run"]["ranking_source"] == "DETERMINISTIC_FALLBACK"
    assert fallback["recommendations"]


def test_ranker_failure_falls_back_and_browsing_still_works(groups_app, client, settings, provider):
    campaign = _setup(client, settings, provider)
    groups_app.state.group_ranker_scenarios.mode = "FAIL"
    view = _recommend(client, settings, campaign)
    assert view["run"]["ranking_source"] == "DETERMINISTIC_FALLBACK"
    assert view["recommendations"]
    browse = client.get(
        f"/api/campaigns/{campaign['id']}/eligible-groups", params={"q": "colorado"}
    )
    assert browse.status_code == 200 and browse.json()["groups"]
    assert all(group["selectable"] for group in browse.json()["groups"])


def test_selection_persists_and_refresh_does_not_change_it(groups_app, client, settings, provider):
    campaign = _setup(client, settings, provider)
    view = _recommend(client, settings, campaign)
    ids = [item["group"]["id"] for item in view["recommendations"][:3]]
    for group_id in ids:
        assert _decide(client, settings, campaign["id"], group_id).status_code == 200
    assert _decide(client, settings, campaign["id"], ids[2], "REJECT").status_code == 200
    refreshed = _recommend(client, settings, campaign)
    selected = {group["id"] for group in refreshed["selection"]["selected"]}
    assert selected == set(ids[:2])
    assert refreshed["selection"]["pending"] is True
    with groups_app.state.database.sessions() as db:
        decisions = db.scalars(select(GroupDecision)).all()
    assert len(decisions) == 4 and all(d.actor_id for d in decisions)
    with groups_app.state.database.sessions() as db:
        alias = db.scalar(
            select(FacebookGroupRecord.id).where(
                FacebookGroupRecord.identity_status == "UNRESOLVED_SHARE_ALIAS"
            )
        )
    response = _decide(client, settings, campaign["id"], str(alias))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GROUP_NOT_ELIGIBLE"


def test_selected_groups_bind_to_approval_and_cannot_expand_it(
    groups_app, client, settings, provider
):
    campaign = _setup(client, settings, provider)
    graphic = client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={
            "expected_revision": client.get(f"/api/campaigns/{campaign['id']}").json()["revision"]
        },
        headers=_headers(client, settings),
    )
    assert graphic.status_code == 200, graphic.text
    view = _recommend(client, settings, campaign)
    abc = [item["group"]["id"] for item in view["recommendations"][:3]]
    extra = view["recommendations"][3]["group"]["id"]
    for group_id in abc:
        _decide(client, settings, campaign["id"], group_id)
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    assert preview["group_selection_pending"] and not preview["can_approve"]
    unsaved = client.post(
        f"/api/campaigns/{campaign['id']}/approve",
        json={
            "expected_revision": preview["campaign"]["revision"],
            "campaign_version_id": preview["version_id"],
            "version_hash": preview["version_hash"],
            "expected_destination_plan_hash": preview["destination_plan_hash"],
        },
        headers=_headers(client, settings),
    )
    assert unsaved.json()["error"]["code"] == "GROUP_SELECTION_NOT_SAVED"

    before = client.get(f"/api/campaigns/{campaign['id']}").json()["current_version"]
    saved = _save(client, settings, campaign["id"])
    version = saved["current_version"]
    assert version["version_number"] == before["version_number"] + 1
    assert sorted(g["group_record_id"] for g in version["facebook_group_targets"]) == sorted(abc)
    assert version["content_hash"] != before["content_hash"]
    assert version["primary_graphic"]["asset_id"] == before["primary_graphic"]["asset_id"]
    assert version["content"] == before["content"]
    assert "facebook_group_targets" in version["revision_targets"]

    approval = _approve_current(client, settings, campaign["id"])
    plan_groups = next(
        t
        for t in approval["destination_plan"]["targets"]
        if t["destination_key"] == "FACEBOOK_GROUPS"
    )["selected_groups"]
    assert sorted(g["group_record_id"] for g in plan_groups) == sorted(abc)

    blocked = _decide(client, settings, campaign["id"], extra)
    assert blocked.json()["error"]["code"] == "GROUP_CHANGE_REQUIRES_NEW_APPROVAL"
    with groups_app.state.database.sessions() as db:
        assert db.get(Campaign, uuid.UUID(campaign["id"])).active_approval_id is not None
    confirmed = _decide(client, settings, campaign["id"], extra, confirm_withdraw_approval=True)
    assert confirmed.status_code == 200
    with groups_app.state.database.sessions() as db:
        row = db.get(Campaign, uuid.UUID(campaign["id"]))
        assert row.active_approval_id is None
        historical = db.get(Approval, uuid.UUID(approval["id"]))
        targets = next(
            t
            for t in historical.destination_plan["targets"]
            if t["destination_key"] == "FACEBOOK_GROUPS"
        )["selected_groups"]
        assert sorted(g["group_record_id"] for g in targets) == sorted(abc)
        with pytest.raises(DBAPIError):
            with groups_app.state.database.sessions.begin() as other:
                other.execute(
                    text("UPDATE approvals SET destination_plan = '{}'::jsonb WHERE id = :id"),
                    {"id": historical.id},
                )
    assert _start(client, settings, approval["id"]).status_code == 409
    successor = _save(client, settings, campaign["id"])["current_version"]
    assert len(successor["facebook_group_targets"]) == 4
    approved_version = client.get(
        f"/api/campaigns/{campaign['id']}/versions/{version['version_number']}"
    ).json()["viewed_version"]
    assert len(approved_version["facebook_group_targets"]) == 3


def test_manual_package_contains_exact_groups_graphic_and_copy(
    groups_app, client, settings, provider
):
    campaign = _setup(client, settings, provider)
    client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={
            "expected_revision": client.get(f"/api/campaigns/{campaign['id']}").json()["revision"]
        },
        headers=_headers(client, settings),
    )
    view = _recommend(client, settings, campaign)
    chosen = [item["group"] for item in view["recommendations"][:2]]
    for group in chosen:
        _decide(client, settings, campaign["id"], group["id"])
    version = _save(client, settings, campaign["id"])["current_version"]
    approval = _approve_current(client, settings, campaign["id"])
    assert _start(client, settings, approval["id"]).status_code == 200
    distribution = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    manual = _executions(distribution, approval["id"])["FACEBOOK_GROUPS"]
    package = manual["manual_package"]
    assert {(g["display_name"], g["url"]) for g in package["selected_groups"]} == {
        (g["display_name"], g["url"]) for g in chosen
    }
    assert package["approved_copy"] == version["content"]["facebook_groups_post"]
    assert package["graphic_asset_id"] == version["primary_graphic"]["asset_id"]
    assert "2 approved groups" in package["group_selection"]
    done = client.post(
        f"/api/destination-executions/{manual['id']}/manual-complete",
        json={},
        headers=_headers(client, settings),
    )
    assert done.status_code == 200, done.text
    after = _executions(
        client.get(f"/api/campaigns/{campaign['id']}/distribution").json(), approval["id"]
    )
    assert after["FACEBOOK_GROUPS"]["status"] == "MANUAL_DONE"


def test_versions_without_groups_keep_their_hash_projection(groups_app, client, settings, provider):
    campaign = _setup(client, settings, provider)
    with groups_app.state.database.sessions() as db:
        version = db.get(CampaignVersion, uuid.UUID(campaign["current_version"]["id"]))
        assert "facebook_group_targets" not in version.snapshot
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    facebook = next(
        t
        for t in preview["destination_plan"]["targets"]
        if t["destination_key"] == "FACEBOOK_GROUPS"
    )
    assert facebook["group_selection_status"] == "DEFERRED_NO_GROUPS_SELECTED"
    assert facebook["selected_groups"] == []


def test_ranker_test_controls_are_absent_outside_test_mode():
    from app.group_ranker import GroupRankerScenario

    with pytest.raises(RuntimeError):
        GroupRankerScenario("production")
    with pytest.raises(RankingFailed):
        scenario = type("S", (), {"mode": "FAIL"})()
        DeterministicGroupRanker(scenario).rank({}, [])
    assert isinstance(DeterministicGroupRanker().rank({}, []), RankingResult)
