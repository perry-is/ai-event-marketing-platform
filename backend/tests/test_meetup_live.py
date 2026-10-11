import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.meetup import EVENT_CREATE, MeetupError, render
from app.models import DestinationExecution, DestinationResourceBinding
from tests.conftest import sign_in
from tests.meetup_fake import (
    GROUP_URLNAME,
    MEMBER_ID,
    NETWORK_URLNAME,
    FakeMeetup,
    write_test_private_key,
)
from tests.test_eventbrite_live import AURORA, _headers, _ready_event


@pytest.fixture
def fake_meetup():
    return FakeMeetup()


@pytest.fixture
def live_meetup_app(app, settings, fake_meetup, tmp_path):
    key_path = tmp_path / "meetup-test-key.pem"
    write_test_private_key(key_path)
    settings.meetup_execution_mode = "LIVE"
    settings.meetup_client_key = "synthetic-client"
    settings.meetup_authorized_member_id = MEMBER_ID
    settings.meetup_signing_key_id = "synthetic-signing-key"
    settings.meetup_private_key_path = str(key_path)
    settings.meetup_network_urlname = NETWORK_URLNAME
    settings.meetup_group_urlname = GROUP_URLNAME
    app.state.execution_service.meetup_http_client = fake_meetup.client()
    app.state.execution_service.rebuild()
    return app


@pytest.fixture
def live_meetup_client(live_meetup_app):
    with TestClient(
        live_meetup_app, base_url="http://localhost:5173", follow_redirects=False
    ) as client:
        yield client


def _setup(client, settings, provider, surfaces=None):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    response = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": event["revision"],
            "changes": [{"field_path": path, "value": value} for path, value in AURORA.items()],
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/api/events/{event['id']}/campaign",
        json={"selected_surfaces": surfaces or ["MEETUP"]},
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _preview(client, campaign_id):
    response = client.get(f"/api/campaigns/{campaign_id}/approval-preview")
    assert response.status_code == 200, response.text
    return response.json()


def _approve(client, settings, campaign_id):
    preview = _preview(client, campaign_id)
    assert preview["can_approve"], preview["destination_readiness"]
    response = client.post(
        f"/api/campaigns/{campaign_id}/approve",
        json={
            "expected_revision": preview["campaign"]["revision"],
            "campaign_version_id": preview["version_id"],
            "version_hash": preview["version_hash"],
            "expected_destination_plan_hash": preview["destination_plan_hash"],
            "replacement_acknowledgements": [
                item["execution_id"] for item in preview["replacement_requirements"]
            ],
            "uncertain_risk_acknowledgements": [],
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _start(client, settings, approval_id):
    response = client.post(
        f"/api/approvals/{approval_id}/start", headers=_headers(client, settings)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _meetup(distribution, approval_id):
    return next(
        row
        for row in distribution["executions"]
        if row["approval_id"] == approval_id and row["destination_key"] == "MEETUP"
    )


def _decode_part(value):
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def test_live_readiness_validates_expected_member_network_and_group(
    live_meetup_client, settings, provider, fake_meetup
):
    sign_in(live_meetup_client, provider)
    response = live_meetup_client.get("/api/settings/meetup")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "READY"
    assert data["checked_live"] is True
    assert data["network_name"] == "Example Network"
    assert data["group_name"] == "Example Business Network"
    assert data["photo_capability"] == "DEFERRED"
    assert "upload awaiting verification" in data["photo_note"]
    fake_meetup.member_id = "99999999"
    mismatched = live_meetup_client.get("/api/settings/meetup").json()
    assert mismatched["status"] == "UNAVAILABLE"
    assert mismatched["reasons"] == ["Connected as an unexpected Meetup account"]
    assert "synthetic-meetup-token" not in response.text
    assert MEMBER_ID not in response.text


def test_jwt_is_short_lived_and_token_never_reaches_status_api(
    live_meetup_client, settings, provider, fake_meetup
):
    sign_in(live_meetup_client, provider)
    response = live_meetup_client.get("/api/settings/meetup")
    assert response.status_code == 200
    header, claims, signature = fake_meetup.assertions[0].split(".")
    assert _decode_part(header) == {
        "alg": "RS256",
        "kid": "synthetic-signing-key",
        "typ": "JWT",
    }
    payload = _decode_part(claims)
    assert payload["iss"] == "synthetic-client"
    assert payload["sub"] == MEMBER_ID
    assert payload["aud"] == "api.meetup.com"
    assert 0 < payload["exp"] - payload["iat"] <= 120
    assert signature
    assert "synthetic-meetup-token" not in response.text
    assert "Authorization" not in response.text


def test_live_create_binds_resource_and_successor_updates_same_meetup_event(
    live_meetup_client, live_meetup_app, settings, provider, fake_meetup
):
    campaign = _setup(live_meetup_client, settings, provider)
    preview = _preview(live_meetup_client, campaign["id"])
    target = preview["destination_plan"]["targets"][0]
    assert target["runtime_mode"] == "LIVE"
    assert target["meetup"]["group_urlname"] == GROUP_URLNAME
    assert target["meetup"]["network_urlname"] == NETWORK_URLNAME
    assert "meetup_event" in target
    approval = _approve(live_meetup_client, settings, campaign["id"])
    distribution = _start(live_meetup_client, settings, approval["id"])
    first = _meetup(distribution, approval["id"])
    assert first["status"] == "SUCCEEDED"
    assert first["verification_status"] == "VERIFIED"
    assert first["external_url"].startswith("https://www.meetup.com/")
    assert fake_meetup.created == 1
    assert fake_meetup.events[first["external_id"]]["status"] == "ACTIVE"
    create_input = next(
        call["variables"]["input"]
        for call in fake_meetup.calls
        if call["operation"] == "MeetupEventCreate"
    )
    assert create_input["duration"] == "PT1H30M"
    assert create_input["selfRsvp"] is False
    assert all(call["operation"] != "MeetupEventPhotoCreate" for call in fake_meetup.calls)

    revised = live_meetup_client.post(
        f"/api/campaigns/{campaign['id']}/revisions",
        json={
            "expected_revision": distribution["campaign"]["revision"],
            "revision_request": "Make Meetup warmer.",
        },
        headers=_headers(live_meetup_client, settings),
    )
    assert revised.status_code == 200, revised.text
    successor = revised.json()
    next_approval = _approve(live_meetup_client, settings, campaign["id"])
    next_distribution = _start(live_meetup_client, settings, next_approval["id"])
    second = _meetup(next_distribution, next_approval["id"])
    assert second["status"] == "SUCCEEDED"
    assert second["external_id"] == first["external_id"]
    assert fake_meetup.created == 1
    assert fake_meetup.updated >= 1
    update_input = next(
        call["variables"]["input"]
        for call in reversed(fake_meetup.calls)
        if call["operation"] == "MeetupEventEdit" and "duration" in call["variables"]["input"]
    )
    assert update_input["duration"] == "5400000"
    assert len(fake_meetup.events) == 1
    with live_meetup_app.state.database.sessions() as db:
        binding = db.scalar(
            select(DestinationResourceBinding).where(
                DestinationResourceBinding.destination_key == "MEETUP"
            )
        )
        assert binding.external_id == first["external_id"]
        executions = db.scalars(
            select(DestinationExecution).where(DestinationExecution.destination_key == "MEETUP")
        ).all()
        assert len(executions) == 2
    assert successor["current_version"]["version_number"] == 2


def test_approved_graphic_blocks_before_any_meetup_mutation(
    live_meetup_client, settings, provider, fake_meetup
):
    campaign = _setup(live_meetup_client, settings, provider)
    graphic = live_meetup_client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={"expected_revision": campaign["revision"]},
        headers=_headers(live_meetup_client, settings),
    )
    assert graphic.status_code == 200, graphic.text
    preview = _preview(live_meetup_client, campaign["id"])
    readiness = next(
        item for item in preview["destination_readiness"] if item["destination_key"] == "MEETUP"
    )
    assert readiness["status"] == "BLOCKED"
    assert readiness["reason"] == "MEETUP_GRAPHIC_UPLOAD_UNVERIFIED"
    assert preview["can_approve"] is False
    rejected = live_meetup_client.post(
        f"/api/campaigns/{campaign['id']}/approve",
        json={
            "expected_revision": preview["campaign"]["revision"],
            "campaign_version_id": preview["version_id"],
            "version_hash": preview["version_hash"],
            "expected_destination_plan_hash": preview["destination_plan_hash"],
            "replacement_acknowledgements": [],
            "uncertain_risk_acknowledgements": [],
        },
        headers=_headers(live_meetup_client, settings),
    )
    assert rejected.status_code == 409
    assert not any(
        call["operation"] in {"MeetupEventCreate", "MeetupEventEdit", "MeetupEventPhotoCreate"}
        for call in fake_meetup.calls
    )
    assert fake_meetup.created == 0


def test_uncertain_create_becomes_unknown_and_is_not_retried(
    live_meetup_client, settings, provider, fake_meetup
):
    campaign = _setup(live_meetup_client, settings, provider)
    fake_meetup.timeout_after_create = True
    approval = _approve(live_meetup_client, settings, campaign["id"])
    distribution = _start(live_meetup_client, settings, approval["id"])
    row = _meetup(distribution, approval["id"])
    assert row["status"] == "UNKNOWN"
    assert row["retryable"] is False
    assert row["external_id"] is None
    retry = live_meetup_client.post(
        f"/api/destination-executions/{row['id']}/retry",
        headers=_headers(live_meetup_client, settings),
    )
    assert retry.status_code == 404 or retry.status_code == 409
    assert fake_meetup.created == 1
    assert len(fake_meetup.events) == 1


def test_old_live_approval_does_not_fall_back_to_simulated_meetup(
    live_meetup_client, live_meetup_app, settings, provider, fake_meetup
):
    campaign = _setup(live_meetup_client, settings, provider)
    approval = _approve(live_meetup_client, settings, campaign["id"])
    execution_service = live_meetup_app.state.execution_service
    execution_service.settings.meetup_execution_mode = "SIMULATED"
    execution_service.rebuild()
    distribution = _start(live_meetup_client, settings, approval["id"])
    row = _meetup(distribution, approval["id"])
    assert row["runtime_mode"] == "LIVE"
    assert row["status"] == "BLOCKED"
    assert row["error_category"] == "APPROVED_CONNECTOR_MODE_UNAVAILABLE"
    assert fake_meetup.created == 0
    assert not any(
        call["operation"] in {"MeetupEventCreate", "MeetupEventEdit"} for call in fake_meetup.calls
    )


def test_create_duration_is_schema_iso_and_event_time_keeps_local_timezone(
    live_meetup_client, settings, provider
):
    campaign = _setup(live_meetup_client, settings, provider)
    target = _preview(live_meetup_client, campaign["id"])["destination_plan"]["targets"][0]
    rendered = render(target)
    assert rendered.start_date_time.endswith("-06:00")
    assert rendered.duration_create == "PT1H30M"
    assert rendered.duration_ms == 5_400_000


def test_graphql_200_rate_limit_is_not_success_and_values_stay_in_variables(
    live_meetup_app, fake_meetup
):
    client = live_meetup_app.state.execution_service.meetup_client()
    fake_meetup.top_error_next = "RATE_LIMITED"
    with pytest.raises(MeetupError) as error:
        client.execute("MeetupEventCreate", EVENT_CREATE, {"input": {}}, mutation=True)
    assert error.value.category == "RATE_LIMIT"
    assert error.value.may_have_applied is False

    marker = '"} mutation Dangerous { deleteEvent(input: {eventId: "1"}) } #'
    result = client.execute(
        "MeetupGroupReadiness",
        "query MeetupGroupReadiness($urlname: String!) "
        "{ groupByUrlname(urlname: $urlname) { urlname } }",
        {"urlname": marker},
    )
    assert result["groupByUrlname"] is None
    call = fake_meetup.calls[-1]
    assert call["variables"]["urlname"] == marker
    assert call["operation"] == "MeetupGroupReadiness"
    assert marker not in call["query"]


def test_missing_key_setup_fails_readiness_without_api_calls(
    live_meetup_client, live_meetup_app, settings, provider, fake_meetup
):
    sign_in(live_meetup_client, provider)
    live_meetup_app.state.execution_service.settings.meetup_private_key_path = ""
    live_meetup_app.state.execution_service.rebuild()
    response = live_meetup_client.get("/api/settings/meetup")
    assert response.status_code == 200
    assert response.json()["status"] == "NEEDS_SETUP"
    assert response.json()["reasons"] == ["Meetup private key not configured"]
    assert fake_meetup.calls == []


def test_network_and_group_mismatch_are_not_ready(
    live_meetup_client, settings, provider, fake_meetup
):
    sign_in(live_meetup_client, provider)
    fake_meetup.network_urlname = "wrong-network"
    network = live_meetup_client.get("/api/settings/meetup").json()
    assert network["status"] == "NEEDS_SETUP"
    assert network["reasons"] == ["Configured Meetup Pro Network is unavailable"]

    fake_meetup.network_urlname = NETWORK_URLNAME
    fake_meetup.group_urlname = "wrong-group"
    group = live_meetup_client.get("/api/settings/meetup").json()
    assert group["status"] == "NEEDS_SETUP"
    assert group["reasons"] == ["Configured Meetup Group is unavailable"]
    assert not any(
        call["operation"] in {"MeetupEventCreate", "MeetupEventEdit"} for call in fake_meetup.calls
    )


def test_simulated_meetup_remains_unchanged_and_does_not_call_meetup(
    live_meetup_client, live_meetup_app, settings, provider, fake_meetup
):
    live_meetup_app.state.execution_service.settings.meetup_execution_mode = "SIMULATED"
    live_meetup_app.state.execution_service.rebuild()
    campaign = _setup(live_meetup_client, settings, provider)
    preview = _preview(live_meetup_client, campaign["id"])
    target = preview["destination_plan"]["targets"][0]
    assert target["runtime_mode"] == "SIMULATED"
    approval = _approve(live_meetup_client, settings, campaign["id"])
    distribution = _start(live_meetup_client, settings, approval["id"])
    assert _meetup(distribution, approval["id"])["status"] == "SUCCEEDED"
    assert fake_meetup.calls == []


def test_payload_validation_error_is_definite_failure_not_unknown(live_meetup_app):
    adapter = next(
        value
        for (
            destination,
            kind,
            mode,
        ), value in live_meetup_app.state.execution_service.adapters.adapters.items()
        if destination == "MEETUP" and kind == "AUTO" and mode == "LIVE"
    )
    with pytest.raises(MeetupError) as error:
        adapter._payload_event(
            {"errors": [{"message": "invalid title", "code": "BAD_USER_INPUT"}]},
            stage="CREATE",
            may_have_applied=True,
        )
    assert error.value.category == "VALIDATION"
    assert error.value.may_have_applied is False
