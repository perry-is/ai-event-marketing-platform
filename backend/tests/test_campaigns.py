import copy
import json
import uuid

import httpx2
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.campaign_ai import OpenAICampaignService, _expected_claims
from app.campaign_service import (
    SUPPORTED_SURFACES,
    CampaignContent,
    DeterministicCampaignService,
    campaign_hash_projection,
    parse_revision_request,
    snapshot_hash,
)
from app.models import Campaign, CampaignVersion, Event
from tests.conftest import sign_in
from tests.openai_test_support import mock_response

NOTES = (
    "Lunch & Learn on October 22, 2026 from 11:30 AM to 1:00 PM "
    "at Example Venue. Timezone America/Denver."
)


def headers(client, settings):
    return {
        "origin": settings.app_origin,
        "x-csrf-token": client.get("/api/me").json()["csrf_token"],
    }


def ready_event(client, settings, provider):
    event_response = client.post(
        "/api/events", json={"text": NOTES}, headers=headers(client, settings)
    )
    assert event_response.status_code == 201, event_response.text
    event = event_response.json()
    accepted = client.post(
        f"/api/events/{event['id']}/accept-proposals",
        json={"expected_revision": event["revision"]},
        headers=headers(client, settings),
    )
    assert accepted.status_code == 200, accepted.text
    event = accepted.json()
    question = next(
        value
        for value in event["clarifications"]
        if value["status"] == "OPEN" and value["field_path"] == "delivery"
    )
    resolved = client.post(
        f"/api/events/{event['id']}/clarifications/{question['id']}",
        json={"expected_revision": event["revision"], "answer": "In person"},
        headers=headers(client, settings),
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["readiness"]["ready"] is True
    return resolved.json()


def generate(client, settings, event, surfaces=None):
    return client.post(
        f"/api/events/{event['id']}/campaign",
        json={"selected_surfaces": surfaces or list(SUPPORTED_SURFACES)},
        headers=headers(client, settings),
    )


def test_campaign_api_requires_authenticated_session(client):
    campaign_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    assert client.get(f"/api/events/{event_id}/campaign").status_code == 401
    assert (
        client.post(
            f"/api/events/{event_id}/campaign", json={"selected_surfaces": ["INSTAGRAM"]}
        ).status_code
        == 401
    )
    assert client.get(f"/api/campaigns/{campaign_id}").status_code == 401
    assert client.get(f"/api/campaigns/{campaign_id}/versions/1").status_code == 401
    assert (
        client.post(
            f"/api/campaigns/{campaign_id}/revisions",
            json={"expected_revision": 1, "revision_request": "Make Instagram warmer."},
        ).status_code
        == 401
    )


def test_unready_event_cannot_generate_campaign(client, settings, provider, app):
    sign_in(client, provider)
    event = client.post("/api/events", json={}, headers=headers(client, settings)).json()
    result = generate(client, settings, event)
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "EVENT_NOT_READY"
    with app.state.database.sessions() as db:
        assert db.scalars(select(Campaign)).all() == []
        assert db.scalars(select(CampaignVersion)).all() == []


def test_generation_revision_and_history_preserve_immutable_versions(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = ready_event(client, settings, provider)
    initial_response = generate(client, settings, event)
    assert initial_response.status_code == 200, initial_response.text
    initial = initial_response.json()
    assert initial["state"] == "REVIEW"
    assert initial["current_version_number"] == 1
    assert initial["current_version"]["source_event_revision"] == event["revision"]
    assert initial["current_version"]["selected_surfaces"] == list(SUPPORTED_SURFACES)
    first_snapshot = copy.deepcopy(initial["current_version"])
    assert first_snapshot["content"]["eventbrite_description"]
    assert "Example Venue" in first_snapshot["content"]["general_description"]
    assert "11:30 AM" in first_snapshot["content"]["general_description"]

    unsupported = client.post(
        f"/api/campaigns/{initial['id']}/revisions",
        json={"expected_revision": 1, "revision_request": "Make Eventbrite viral."},
        headers=headers(client, settings),
    )
    assert unsupported.status_code == 422
    assert client.get(f"/api/campaigns/{initial['id']}").json()["current_version_number"] == 1

    revision = client.post(
        f"/api/campaigns/{initial['id']}/revisions",
        json={"expected_revision": 1, "revision_request": "Make LinkedIn warmer."},
        headers=headers(client, settings),
    )
    assert revision.status_code == 200, revision.text
    second = revision.json()
    assert second["current_version_number"] == 2
    assert second["versions"][0]["revision_request"] == "Make LinkedIn warmer."
    assert second["current_version"]["primary_graphic"] is None
    current = second["current_version"]["content"]
    assert (
        current["linkedin_organization_post"]
        != first_snapshot["content"]["linkedin_organization_post"]
    )
    assert current["linkedin_personal_post"] != first_snapshot["content"]["linkedin_personal_post"]
    assert current["eventbrite_description"] == first_snapshot["content"]["eventbrite_description"]
    assert second["current_version"]["parent_version_id"] == first_snapshot["id"]
    history = client.get(f"/api/campaigns/{initial['id']}/versions/1")
    assert history.status_code == 200
    viewed_first = history.json()["viewed_version"]
    assert {key: value for key, value in viewed_first.items() if key != "is_current"} == {
        key: value for key, value in first_snapshot.items() if key != "is_current"
    }
    assert viewed_first["is_current"] is False
    with app.state.database.sessions() as db:
        first_row = db.scalar(
            select(CampaignVersion).where(
                CampaignVersion.campaign_id == uuid.UUID(initial["id"]),
                CampaignVersion.version_number == 1,
            )
        )
        assert first_row.canonical_snapshot_text
        with pytest.raises(DBAPIError, match="campaign versions are immutable"):
            with db.begin_nested():
                first_row.snapshot = {"changed": True}
                db.flush()
    with app.state.database.sessions() as db:
        versions = db.scalars(
            select(CampaignVersion)
            .where(CampaignVersion.campaign_id == uuid.UUID(initial["id"]))
            .order_by(CampaignVersion.version_number)
        ).all()
        assert versions[0].snapshot["content"] == first_snapshot["content"]
        with pytest.raises(DBAPIError, match="campaign versions are immutable"):
            with db.begin_nested():
                db.delete(versions[0])
                db.flush()


def test_openai_campaign_versions_preserve_event_history_and_require_fresh_approval(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = ready_event(client, settings, provider)
    requests = []
    failures = {"generation": True, "revision": False}

    def provider_response(request):
        body = json.loads(request.content)
        payload = json.loads(body["input"][0]["content"])
        requests.append((body, payload))
        if payload.get("revision_request") and failures["revision"]:
            return mock_response({"content": {}, "factual_claims": []})
        if not payload.get("revision_request") and failures["generation"]:
            return mock_response({"content": {}, "factual_claims": []})
        service = DeterministicCampaignService()
        if payload.get("revision_request"):
            current = CampaignContent.model_validate(payload["current_content"], strict=True)
            content, _ = service.revise(
                current,
                payload["locked_event_facts"],
                payload["revision_request"],
                payload["selected_surfaces"],
            )
        else:
            content = service.generate(payload["locked_event_facts"], payload["selected_surfaces"])
        output = {
            "content": content.model_dump(mode="json"),
            "factual_claims": [
                {"field_path": path, "value_json": value}
                for path, value in _expected_claims(payload["locked_event_facts"]).items()
            ],
        }
        return mock_response(output, response_id=f"resp_campaign_{len(requests)}")

    provider_client = httpx2.Client(transport=httpx2.MockTransport(provider_response))
    ai = OpenAICampaignService(
        api_key="test-provider-key", model_id="gpt-6-luna-test", http_client=provider_client
    )
    app.state.campaign_service = ai
    try:
        failed_generation = generate(client, settings, event, ["INSTAGRAM"])
        assert failed_generation.status_code == 503
        assert failed_generation.json()["error"]["code"] == "CAMPAIGN_AI_RESPONSE_INVALID"
        with app.state.database.sessions() as db:
            assert db.scalars(select(Campaign)).all() == []
            assert db.scalars(select(CampaignVersion)).all() == []

        failures["generation"] = False
        created_response = generate(client, settings, event, ["INSTAGRAM"])
        assert created_response.status_code == 200, created_response.text
        first = created_response.json()
        first_view = copy.deepcopy(first["current_version"])
        assert first_view["generator_id"] == "openai-responses-campaign"
        assert first_view["generator_version"] == "campaign-generation-v3"
        first_snapshot = _load_version_snapshot(app, first_view["id"])
        assert first_view["content_hash"] == snapshot_hash(first_snapshot)
        assert first_snapshot["generation"]["model_id"] == "gpt-6-luna-test"
        assert first_snapshot["generation"]["prompt_version"] == "campaign-generation-v3"
        assert len(requests) == 2
        assert requests[0][0]["store"] is False
        assert requests[1][0]["store"] is False

        preview = client.get(f"/api/campaigns/{first['id']}/approval-preview").json()
        approval_response = client.post(
            f"/api/campaigns/{first['id']}/approve",
            json={
                "expected_revision": preview["campaign"]["revision"],
                "campaign_version_id": preview["version_id"],
                "version_hash": preview["version_hash"],
                "expected_destination_plan_hash": preview["destination_plan_hash"],
                "replacement_acknowledgements": [],
                "uncertain_risk_acknowledgements": [],
            },
            headers=headers(client, settings),
        )
        assert approval_response.status_code == 200, approval_response.text
        approval_id = approval_response.json()["id"]
        approved_campaign = client.get(f"/api/campaigns/{first['id']}").json()

        failures["revision"] = True
        failed_revision = client.post(
            f"/api/campaigns/{first['id']}/revisions",
            json={
                "expected_revision": approved_campaign["revision"],
                "revision_request": "Make Instagram warmer.",
            },
            headers=headers(client, settings),
        )
        assert failed_revision.status_code == 503
        assert failed_revision.json()["error"]["code"] == "CAMPAIGN_REVISION_FAILED"
        preserved = client.get(f"/api/campaigns/{first['id']}").json()
        assert preserved["current_version_number"] == 1
        assert preserved["current_version"]["id"] == first_view["id"]
        assert (
            client.get(f"/api/campaigns/{first['id']}/distribution").json()["active_approval"]["id"]
            == approval_id
        )

        failures["revision"] = False
        approved_campaign = client.get(f"/api/campaigns/{first['id']}").json()
        revised_response = client.post(
            f"/api/campaigns/{first['id']}/revisions",
            json={
                "expected_revision": approved_campaign["revision"],
                "revision_request": "Make Instagram warmer.",
            },
            headers=headers(client, settings),
        )
        assert revised_response.status_code == 200, revised_response.text
        second = revised_response.json()
        assert second["current_version_number"] == 2
        assert second["current_version"]["generator_version"] == "campaign-revision-v3"
        assert (
            second["current_version"]["content"]["instagram_caption"]
            != first_view["content"]["instagram_caption"]
        )
        assert len(requests) == 4
        assert requests[2][0]["max_output_tokens"] == 6500
        assert requests[3][1]["allowed_fields"] == ["instagram_caption"]

        history = client.get(f"/api/campaigns/{first['id']}/versions/1").json()["viewed_version"]
        assert {key: value for key, value in history.items() if key != "is_current"} == {
            key: value for key, value in first_view.items() if key != "is_current"
        }
        distribution = client.get(f"/api/campaigns/{first['id']}/distribution").json()
        assert distribution["active_approval"] is None
        assert distribution["executions"] == []
        assert len(distribution["approvals"]) == 1
        assert distribution["approvals"][0]["id"] == approval_id
        next_preview = client.get(f"/api/campaigns/{first['id']}/approval-preview").json()
        assert next_preview["version_id"] == second["current_version"]["id"]
        assert next_preview["version_id"] != approval_response.json()["campaign_version_id"]

        with app.state.database.sessions() as db:
            persisted_event = db.get(Event, uuid.UUID(event["id"]))
            version_one = db.scalar(
                select(CampaignVersion).where(
                    CampaignVersion.campaign_id == uuid.UUID(first["id"]),
                    CampaignVersion.version_number == 1,
                )
            )
            assert persisted_event.title == event["title"]
            assert persisted_event.revision == event["revision"]
            assert version_one.snapshot == first_snapshot
            assert version_one.content_hash == first_view["content_hash"]
    finally:
        ai.close()


def _load_version_snapshot(app, version_id):
    with app.state.database.sessions() as db:
        version = db.scalar(
            select(CampaignVersion).where(CampaignVersion.id == uuid.UUID(version_id))
        )
        return version.snapshot


def test_event_fact_revision_is_redirected_and_material_edits_mark_campaign_stale(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = ready_event(client, settings, provider)
    campaign = generate(client, settings, event).json()
    campaign_id = campaign["id"]
    fact_request = client.post(
        f"/api/campaigns/{campaign_id}/revisions",
        json={
            "expected_revision": 1,
            "revision_request": "Change the date to October 24.",
        },
        headers=headers(client, settings),
    )
    assert fact_request.status_code == 409
    assert fact_request.json()["error"]["code"] == "EVENT_CORRECTION_REQUIRED"
    assert client.get(f"/api/campaigns/{campaign_id}").json()["current_version_number"] == 1

    # A purely administrative revision is provenance-only and does not make review stale.
    with app.state.database.sessions.begin() as db:
        row = db.get(Event, uuid.UUID(event["id"]))
        row.internal_notes = "Private operator reminder"
        row.revision += 1
    still_current = client.get(f"/api/campaigns/{campaign_id}").json()
    assert still_current["is_stale"] is False

    edit = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": event["revision"] + 1,
            "changes": [{"field_path": "title", "value": "Community Learning Lunch"}],
        },
        headers=headers(client, settings),
    )
    assert edit.status_code == 200, edit.text
    stale = client.get(f"/api/campaigns/{campaign_id}").json()
    assert stale["is_stale"] is True
    assert "changed since" in stale["stale_message"]
    blocked_revision = client.post(
        f"/api/campaigns/{campaign_id}/revisions",
        json={"expected_revision": stale["revision"], "revision_request": "Make LinkedIn warmer."},
        headers=headers(client, settings),
    )
    assert blocked_revision.status_code == 409
    assert blocked_revision.json()["error"]["code"] == "CAMPAIGN_STALE_REGENERATE_FIRST"
    regenerate = client.post(
        f"/api/campaigns/{campaign_id}/regenerate",
        json={"expected_revision": stale["revision"]},
        headers=headers(client, settings),
    )
    assert regenerate.status_code == 200, regenerate.text
    refreshed = regenerate.json()
    assert refreshed["current_version_number"] == 2
    assert refreshed["is_stale"] is False
    assert refreshed["current_version"]["event_snapshot"]["title"] == "Community Learning Lunch"


def test_campaigns_do_not_impose_one_to_one_event_cardinality(client, settings, provider, app):
    sign_in(client, provider)
    event = ready_event(client, settings, provider)
    campaign = generate(client, settings, event).json()
    with app.state.database.sessions.begin() as db:
        original = db.get(Campaign, uuid.UUID(campaign["id"]))
        second = Campaign(
            id=uuid.uuid4(),
            event_id=original.event_id,
            state="DRAFT",
            revision=1,
            current_version_id=None,
            created_by=original.created_by,
            created_at=original.created_at,
            updated_at=original.updated_at,
        )
        db.add(second)
    with app.state.database.sessions() as db:
        rows = db.scalars(select(Campaign).where(Campaign.event_id == uuid.UUID(event["id"]))).all()
        assert len(rows) == 2


def test_failed_generation_and_revision_leave_no_partial_version(client, settings, provider, app):
    sign_in(client, provider)
    event = ready_event(client, settings, provider)

    class InvalidGenerator:
        def generate(self, event_snapshot, selected_surfaces):
            return {"unvalidated": True}

        def revise(self, current, event_snapshot, revision_request, selected_surfaces):
            raise RuntimeError("synthetic failure")

    app.state.campaign_service = InvalidGenerator()
    failed = generate(client, settings, event)
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "CAMPAIGN_GENERATION_FAILED"
    with app.state.database.sessions() as db:
        assert db.scalars(select(Campaign)).all() == []
        assert db.scalars(select(CampaignVersion)).all() == []

    app.state.campaign_service = DeterministicCampaignService()
    created = generate(client, settings, event).json()
    app.state.campaign_service = InvalidGenerator()
    failed_revision = client.post(
        f"/api/campaigns/{created['id']}/revisions",
        json={"expected_revision": 1, "revision_request": "Make LinkedIn warmer."},
        headers=headers(client, settings),
    )
    assert failed_revision.status_code == 503
    assert failed_revision.json()["error"]["code"] == "CAMPAIGN_REVISION_FAILED"
    unchanged = client.get(f"/api/campaigns/{created['id']}").json()
    assert unchanged["current_version_number"] == 1
    assert len(unchanged["versions"]) == 1


def test_event_change_during_generation_rejects_the_captured_campaign_snapshot(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = ready_event(client, settings, provider)

    class EventChangingGenerator:
        def generate(self, event_snapshot, selected_surfaces):
            with app.state.database.sessions.begin() as db:
                current = db.get(Event, uuid.UUID(event_snapshot["id"]))
                current.title = "Changed while copy was prepared"
                current.revision += 1
            return DeterministicCampaignService().generate(event_snapshot, selected_surfaces)

    app.state.campaign_service = EventChangingGenerator()
    result = generate(client, settings, event)
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "CAMPAIGN_INPUTS_CHANGED"
    with app.state.database.sessions() as db:
        assert db.scalars(select(Campaign)).all() == []
        assert db.scalars(select(CampaignVersion)).all() == []


def test_snapshot_hash_tracks_business_content_but_excludes_metadata():
    snapshot = {
        "schema_version": 1,
        "event": {"id": "event-1", "revision": 3, "title": "Lunch & Learn"},
        "selected_surfaces": ["INSTAGRAM", "LINKEDIN_PERSONAL"],
        "content": {"marketing_headline": "Lunch & Learn", "instagram_caption": "Join us"},
        "generation": {"generator_id": "template-a"},
    }
    equivalent = copy.deepcopy(snapshot)
    equivalent["event"]["id"] = "other-id"
    equivalent["event"]["revision"] = 99
    equivalent["generation"] = {"generator_id": "other"}
    assert snapshot_hash(snapshot) == snapshot_hash(equivalent)
    assert campaign_hash_projection(snapshot)["event_facts"] == {"title": "Lunch & Learn"}
    changed = copy.deepcopy(snapshot)
    changed["content"]["instagram_caption"] = "A different message"
    assert snapshot_hash(snapshot) != snapshot_hash(changed)
    changed = copy.deepcopy(snapshot)
    changed["selected_surfaces"].reverse()
    assert snapshot_hash(snapshot) != snapshot_hash(changed)


def test_deterministic_content_uses_known_schedule_people_price_and_registration_facts():
    snapshot = {
        "title": "Practical Growth Workshop",
        "starts_at": "2026-10-22T17:30:00+00:00",
        "ends_at": "2026-10-23T01:00:00+00:00",
        "timezone": "America/Denver",
        "delivery": "VIRTUAL",
        "facts": {
            "topic": "business planning",
            "location": {"join_instructions": "Join link arrives after signup"},
            "people": {"host": "Alex Rivera", "presenters": "Jordan Lee"},
            "registration": {
                "price_kind": "PAID",
                "amount_minor": 2500,
                "currency": "USD",
                "strategy": "PROVIDED_URL",
                "url": "https://example.test/register",
            },
            "content_facts": {},
        },
    }
    generated = DeterministicCampaignService().generate(snapshot, ["EVENTBRITE"])
    assert isinstance(generated, CampaignContent)
    assert "Thursday, October 22, 2026" in generated.general_description
    assert " – 7:00 PM" in generated.general_description
    assert "Format: Virtual" in generated.general_description
    assert "Where: Join link arrives after signup" not in generated.general_description
    assert "Host: Alex Rivera" in generated.general_description
    assert "Presenters: Jordan Lee" in generated.general_description
    assert "Admission: USD 25.00" in generated.general_description
    assert "https://example.test/register" in generated.general_description


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("Make LinkedIn warmer.", {"linkedin_organization_post", "linkedin_personal_post"}),
        ("Shorten the Eventbrite description.", {"eventbrite_description"}),
        ("Make Instagram more energetic.", {"instagram_caption"}),
        ("Make the title less formal.", {"marketing_headline"}),
    ],
)
def test_deterministic_revision_commands_target_only_intended_content(instruction, expected):
    assert set(parse_revision_request(instruction, list(SUPPORTED_SURFACES))) == expected


@pytest.mark.parametrize(
    "instruction",
    [
        "Change the date to October 24.",
        "Actually this is virtual.",
        "Change the price to $25.",
    ],
)
def test_protected_event_fact_changes_are_not_marketing_revisions(instruction):
    from app.campaign_service import is_event_fact_revision

    assert is_event_fact_revision(instruction)


def test_revision_parser_rejects_unsupported_instructions():
    with pytest.raises(HTTPException) as error:
        parse_revision_request(
            "Add three hashtags and mention a discount.", list(SUPPORTED_SURFACES)
        )
    assert error.value.status_code == 422
