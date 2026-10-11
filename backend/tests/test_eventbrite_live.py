import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.config import Settings
from app.main import create_app
from app.models import DestinationExecution, DestinationResourceBinding, GeneratedAsset
from tests.conftest import sign_in
from tests.eventbrite_fake import ORGANIZATION_ID, TOKEN, FakeEventbrite, live_settings_values
from tests.test_execution import _headers, _ready_event

AURORA = {
    "facts.location.venue": "Aurora Event Center",
    "facts.location.address": "123 Main St, Aurora, CO 80012",
    "facts.registration.capacity": "120",
    "facts.registration.price_kind": "FREE",
}


@pytest.fixture
def fake():
    return FakeEventbrite()


@pytest.fixture
def live_app(app, settings, provider, fake):
    values = settings.model_dump() | live_settings_values()
    live_settings = Settings(_env_file=None, **values)
    return create_app(live_settings, provider.oidc, eventbrite_http_client=fake.client())


@pytest.fixture
def live_client(live_app):
    from fastapi.testclient import TestClient

    with TestClient(live_app, base_url="http://localhost:5173", follow_redirects=False) as client:
        yield client


def _setup(client, settings, provider, facts=None, surfaces=None):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    changes = [{"field_path": path, "value": value} for path, value in (facts or AURORA).items()]
    response = client.patch(
        f"/api/events/{event['id']}/facts",
        json={"expected_revision": event["revision"], "changes": changes},
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/api/events/{event['id']}/campaign",
        json={
            "selected_surfaces": surfaces
            or ["EVENTBRITE", "MEETUP", "LINKEDIN_ORGANIZATION", "INSTAGRAM", "FACEBOOK_GROUPS"]
        },
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
    return client.post(f"/api/approvals/{approval_id}/start", headers=_headers(client, settings))


def _rows(distribution, approval_id):
    return {
        row["destination_key"]: row
        for row in distribution["executions"]
        if row["approval_id"] == approval_id
    }


def _eventbrite_entry(plan):
    return next(t for t in plan["targets"] if t["destination_key"] == "EVENTBRITE")


def _readiness(preview, destination="EVENTBRITE"):
    return next(r for r in preview["destination_readiness"] if r["destination_key"] == destination)


# --- Readiness / Settings --------------------------------------------------------------------


def test_settings_readiness_states_never_expose_credentials(
    live_client, live_app, settings, provider, fake
):
    from pydantic import SecretStr

    sign_in(live_client, provider)
    configured = live_app.state.settings

    def status(**overrides):
        for key, value in live_settings_values().items():
            setattr(configured, key, SecretStr(value) if key.endswith("token") else value)
        for key, value in overrides.items():
            setattr(configured, key, SecretStr(value) if key.endswith("token") else value)
        response = live_client.get("/api/settings/eventbrite")
        assert response.status_code == 200
        assert TOKEN not in response.text and ORGANIZATION_ID not in response.text
        assert "2000002" not in response.text
        return response.json()

    missing = status(eventbrite_private_token="")
    assert missing["status"] == "NEEDS_SETUP"
    assert "Access token not configured" in missing["reasons"]
    no_organizer = status(eventbrite_organizer_id="")
    assert "Organizer not configured" in no_organizer["reasons"]
    fake.fail_token = True
    rejected = status()
    assert rejected["status"] == "UNAVAILABLE"
    assert rejected["reasons"] == ["Eventbrite rejected the access token"]
    fake.fail_token = False
    fake.organizations = []
    assert status()["reasons"] == ["Configured organization isn't available to this token"]
    fake.reset()
    fake.organizers = []
    assert status()["reasons"] == ["Configured organizer isn't part of this organization"]
    fake.reset()
    ready = status()
    assert ready["status"] == "READY" and ready["checked_live"]
    assert ready["organization_name"] == "Event Marketing"
    assert all(method == "GET" for method, _ in fake.calls)  # readiness never writes


def test_settings_and_execution_routes_reject_anonymous(live_client):
    assert live_client.get("/api/settings/eventbrite").status_code == 401
    assert live_client.post(f"/api/approvals/{uuid.uuid4()}/start").status_code == 401


# --- Mixed plan and exact create -------------------------------------------------------------


def test_live_plan_creates_once_binds_and_verifies(live_client, live_app, settings, provider, fake):
    campaign = _setup(live_client, settings, provider)
    preview = _preview(live_client, campaign["id"])
    entry = _eventbrite_entry(preview["destination_plan"])
    assert entry["runtime_mode"] == "LIVE"
    assert entry["target_key"] == f"eventbrite-organization:{ORGANIZATION_ID}"
    assert entry["eventbrite"]["listing"]["capacity"] == 120
    others = {
        t["destination_key"]: t.get("runtime_mode") for t in preview["destination_plan"]["targets"]
    }
    assert others["MEETUP"] == "SIMULATED" and others["FACEBOOK_GROUPS"] is None
    assert TOKEN not in str(preview)

    approval = _approve(live_client, settings, campaign["id"])
    response = _start(live_client, settings, approval["id"])
    assert response.status_code == 200, response.text
    rows = _rows(response.json(), approval["id"])
    eventbrite = rows["EVENTBRITE"]
    assert eventbrite["status"] == "SUCCEEDED", eventbrite
    assert eventbrite["external_url"].startswith("https://www.eventbrite.com/e/")
    assert rows["MEETUP"]["status"] == "SUCCEEDED"
    assert rows["MEETUP"]["external_url"].startswith("https://simulated.invalid/")
    assert fake.creates() == 1
    event = next(iter(fake.events.values()))
    assert event["status"] == "live" and event["organizer_id"] == "2000002"
    assert event["name_text"] == "Lunch & Learn" and event["online_event"] is False
    ticket = next(iter(fake.tickets.values()))
    assert ticket["free"] is True and ticket["quantity_total"] == 120
    approved_copy = preview["destination_plan"]["targets"][0]
    copy_text = _eventbrite_entry(preview["destination_plan"])["approved_content"]
    module_html = next(iter(fake.content.values()))["modules"][0]["data"]["body"]["text"]
    assert "Lunch &amp; Learn" in module_html and approved_copy
    assert copy_text.split("\n")[0].replace("&", "&amp;") in module_html
    with live_app.state.database.sessions() as db:
        binding = db.scalar(select(DestinationResourceBinding))
        assert binding.external_id == eventbrite["external_id"]
        assert binding.state == "PUBLISHED" and binding.last_verified_at is not None
    # Repeating start for the same approval publishes nothing again.
    again = _start(live_client, settings, approval["id"])
    assert again.status_code == 200 and fake.creates() == 1
    with live_app.state.database.sessions() as db, pytest.raises(DBAPIError):
        with live_app.state.database.sessions.begin() as other:
            other.execute(text("UPDATE destination_resource_bindings SET external_id = '1'"))


def test_uncertain_create_becomes_unknown_and_never_duplicates(
    live_client, settings, provider, fake
):
    campaign = _setup(live_client, settings, provider)
    approval = _approve(live_client, settings, campaign["id"])
    fake.timeout_after_create = True
    rows = _rows(_start(live_client, settings, approval["id"]).json(), approval["id"])
    eventbrite = rows["EVENTBRITE"]
    assert eventbrite["status"] == "UNKNOWN"
    assert "may have created" in eventbrite["human_error"]
    assert fake.creates() == 1
    retry = live_client.post(
        f"/api/destination-executions/{eventbrite['id']}/retry",
        headers=_headers(live_client, settings),
    )
    assert retry.status_code == 409
    reconciled = live_client.post(
        f"/api/destination-executions/{eventbrite['id']}/reconcile",
        headers=_headers(live_client, settings),
    )
    assert reconciled.status_code == 200
    after = _rows(reconciled.json(), approval["id"])["EVENTBRITE"]
    assert after["status"] == "UNKNOWN"  # no ID known: never guessed from title/date
    assert fake.creates() == 1


def test_successor_version_updates_same_remote_event(live_client, settings, provider, fake):
    campaign = _setup(live_client, settings, provider)
    first = _approve(live_client, settings, campaign["id"])
    first_rows = _rows(_start(live_client, settings, first["id"]).json(), first["id"])
    external_id = first_rows["EVENTBRITE"]["external_id"]
    revised = live_client.post(
        f"/api/campaigns/{campaign['id']}/revisions",
        json={
            "expected_revision": live_client.get(f"/api/campaigns/{campaign['id']}").json()[
                "revision"
            ],
            "revision_request": "Make Eventbrite shorter.",
        },
        headers=_headers(live_client, settings),
    )
    assert revised.status_code == 200, revised.text
    second = _approve(live_client, settings, campaign["id"])
    rows = _rows(_start(live_client, settings, second["id"]).json(), second["id"])
    assert rows["EVENTBRITE"]["status"] == "SUCCEEDED"
    assert rows["EVENTBRITE"]["external_id"] == external_id
    assert fake.creates() == 1
    assert ("POST", f"/events/{external_id}/") in fake.calls
    assert fake.content[external_id]["version"] == 2
    distribution = live_client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    assert _rows(distribution, first["id"])["EVENTBRITE"]["status"] == "SUCCEEDED"
    assert len(fake.tickets) == 1  # the same ticket class is updated, not duplicated


# --- Venue / tickets / blocking preflight ----------------------------------------------------


def test_virtual_event_creates_no_venue(live_client, settings, provider, fake):
    facts = {
        "delivery": "Virtual",
        "facts.location.join_url": "https://meet.example.test/lunch",
        "facts.registration.capacity": "50",
        "facts.registration.price_kind": "FREE",
    }
    campaign = _setup(live_client, settings, provider, facts=facts)
    approval = _approve(live_client, settings, campaign["id"])
    rows = _rows(_start(live_client, settings, approval["id"]).json(), approval["id"])
    assert rows["EVENTBRITE"]["status"] == "SUCCEEDED"
    assert not fake.venues and not any("/venues/" in path for _, path in fake.calls)
    assert next(iter(fake.events.values()))["online_event"] is True


def test_venue_reused_only_on_strong_match(live_client, settings, provider, fake):
    fake.venues["900"] = {
        "id": "900",
        "name": "Aurora Event Center",
        "address": {"address_1": "123 Main St", "city": "Aurora", "region": "CO",
                    "postal_code": "80012"},
    }  # fmt: skip
    fake.venues["901"] = {
        "id": "901",
        "name": "Aurora Event Center",
        "address": {"address_1": "9 Other Rd", "city": "Aurora", "region": "CO"},
    }
    campaign = _setup(live_client, settings, provider)
    approval = _approve(live_client, settings, campaign["id"])
    _start(live_client, settings, approval["id"])
    assert next(iter(fake.events.values()))["venue_id"] == "900"
    assert len(fake.venues) == 2  # nothing created

    fake.reset()
    fake.venues["901"] = {
        "id": "901",
        "name": "Aurora Event Center",
        "address": {"address_1": "9 Other Rd", "city": "Aurora", "region": "CO"},
    }
    campaign2 = _setup(live_client, settings, provider)
    approval2 = _approve(live_client, settings, campaign2["id"])
    _start(live_client, settings, approval2["id"])
    event = next(iter(fake.events.values()))
    assert event["venue_id"] not in {"901"} and len(fake.venues) == 2


def test_missing_capacity_and_paid_tickets_block_before_approval(live_client, settings, provider):
    no_capacity = dict(AURORA)
    no_capacity.pop("facts.registration.capacity")
    campaign = _setup(live_client, settings, provider, facts=no_capacity)
    preview = _preview(live_client, campaign["id"])
    assert _readiness(preview)["status"] == "BLOCKED"
    assert _readiness(preview)["reason"] == "EVENTBRITE_CAPACITY_REQUIRED"
    assert not preview["can_approve"]

    paid = dict(AURORA, **{"facts.registration.price_kind": "PAID"})
    campaign = _setup(live_client, settings, provider, facts=paid)
    preview = _preview(live_client, campaign["id"])
    assert _readiness(preview)["reason"] == "EVENTBRITE_PAID_TICKETS_UNSUPPORTED"

    no_address = dict(AURORA)
    no_address.pop("facts.location.address")
    campaign = _setup(live_client, settings, provider, facts=no_address)
    assert _readiness(_preview(live_client, campaign["id"]))["reason"] == (
        "EVENTBRITE_ADDRESS_REQUIRED"
    )


# --- Graphic, publish failure, retry, drift --------------------------------------------------


def test_exact_approved_graphic_bytes_are_uploaded(live_client, live_app, settings, provider, fake):
    campaign = _setup(live_client, settings, provider)
    graphic = live_client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={
            "expected_revision": live_client.get(f"/api/campaigns/{campaign['id']}").json()[
                "revision"
            ]
        },
        headers=_headers(live_client, settings),
    )
    assert graphic.status_code == 200, graphic.text
    approved_version = graphic.json()["current_version"]
    approval = _approve(live_client, settings, campaign["id"])
    rows = _rows(_start(live_client, settings, approval["id"]).json(), approval["id"])
    assert rows["EVENTBRITE"]["status"] == "SUCCEEDED"
    from app.graphics_service import _read_asset

    with live_app.state.database.sessions() as db:
        asset = db.get(GeneratedAsset, uuid.UUID(approved_version["primary_graphic"]["asset_id"]))
        approved_bytes = _read_asset(live_app.state.settings, asset)
    uploaded = next(iter(fake.uploads.values()))
    assert uploaded == approved_bytes
    assert all("authorization" not in h for h in fake.upload_headers)  # token stays private
    event = next(iter(fake.events.values()))
    assert event["logo_id"] in fake.media

    # A newer unapproved graphic version cannot be published by the old approval.
    newer = live_client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={
            "expected_revision": live_client.get(f"/api/campaigns/{campaign['id']}").json()[
                "revision"
            ],
            "instruction": "Use less orange.",
        },
        headers=_headers(live_client, settings),
    )
    assert newer.status_code == 200
    calls_before = len(fake.calls)
    assert _start(live_client, settings, approval["id"]).status_code == 409
    assert len(fake.calls) == calls_before


def test_publish_rejection_is_failure_and_retry_updates_same_listing(
    live_client, live_app, settings, provider, fake
):
    campaign = _setup(live_client, settings, provider)
    approval = _approve(live_client, settings, campaign["id"])
    fake.reject_publish = True
    rows = _rows(_start(live_client, settings, approval["id"]).json(), approval["id"])
    eventbrite = rows["EVENTBRITE"]
    assert eventbrite["status"] == "FAILED" and eventbrite["error_category"] == "VALIDATION"
    assert "same listing" in eventbrite["result_summary"]
    meetup_attempts = len(rows["MEETUP"]["attempts"])
    with live_app.state.database.sessions() as db:
        assert db.scalar(select(DestinationResourceBinding)).state == "DRAFT"
    fake.reject_publish = False
    retried = live_client.post(
        f"/api/destination-executions/{eventbrite['id']}/retry",
        headers=_headers(live_client, settings),
    )
    assert retried.status_code == 200, retried.text
    after = _rows(retried.json(), approval["id"])
    assert after["EVENTBRITE"]["status"] == "SUCCEEDED"
    assert fake.creates() == 1
    assert len(after["MEETUP"]["attempts"]) == meetup_attempts  # siblings untouched
    with live_app.state.database.sessions() as db:
        execution = db.get(DestinationExecution, uuid.UUID(eventbrite["id"]))
        assert execution.runtime_mode == "LIVE"


def test_rate_limit_is_retryable_without_duplicates(live_client, settings, provider, fake):
    campaign = _setup(live_client, settings, provider)
    approval = _approve(live_client, settings, campaign["id"])
    fake.rate_limit_paths = {f"/organizations/{ORGANIZATION_ID}/events/"}
    eventbrite = _rows(_start(live_client, settings, approval["id"]).json(), approval["id"])[
        "EVENTBRITE"
    ]
    assert eventbrite["status"] == "FAILED" and eventbrite["error_category"] == "RATE_LIMIT"
    assert eventbrite["retryable"] is True and not fake.events
    fake.rate_limit_paths = set()
    retried = live_client.post(
        f"/api/destination-executions/{eventbrite['id']}/retry",
        headers=_headers(live_client, settings),
    )
    assert _rows(retried.json(), approval["id"])["EVENTBRITE"]["status"] == "SUCCEEDED"
    assert len(fake.events) == 1


def test_config_drift_never_silently_simulates(live_client, live_app, settings, provider, fake):
    campaign = _setup(live_client, settings, provider)
    approval = _approve(live_client, settings, campaign["id"])
    live_app.state.settings.eventbrite_execution_mode = "SIMULATED"
    live_app.state.execution_service.rebuild()
    rows = _rows(_start(live_client, settings, approval["id"]).json(), approval["id"])
    assert rows["EVENTBRITE"]["status"] == "BLOCKED"
    assert rows["EVENTBRITE"]["error_category"] == "APPROVED_CONNECTOR_MODE_UNAVAILABLE"
    assert rows["EVENTBRITE"]["runtime_mode"] == "LIVE"
    assert rows["MEETUP"]["status"] == "SUCCEEDED"
    assert fake.calls == []


def test_capacity_correction_is_validated_and_not_from_extraction(live_client, settings, provider):
    sign_in(live_client, provider)
    event = _ready_event(live_client, settings)
    for bad in ("0", "-3", "lots", "2000000"):
        response = live_client.patch(
            f"/api/events/{event['id']}/facts",
            json={
                "expected_revision": event["revision"],
                "changes": [{"field_path": "facts.registration.capacity", "value": bad}],
            },
            headers=_headers(live_client, settings),
        )
        assert response.status_code == 422
    from app.event_service import CORRECTION_ONLY_PATHS, FIELD_PATHS

    assert "facts.registration.capacity" in CORRECTION_ONLY_PATHS
    assert "facts.registration.capacity" not in FIELD_PATHS


def test_one_remote_listing_cannot_be_claimed_by_two_local_events(
    live_client, live_app, settings, provider, fake
):
    from sqlalchemy.exc import IntegrityError

    campaign = _setup(live_client, settings, provider)
    approval = _approve(live_client, settings, campaign["id"])
    _start(live_client, settings, approval["id"])
    second = _setup(live_client, settings, provider)
    with live_app.state.database.sessions() as db:
        bound = db.scalar(select(DestinationResourceBinding))
        other_event = db.execute(
            text("SELECT event_id FROM campaigns WHERE id = :id"), {"id": second["id"]}
        ).scalar_one()
    from app.eventbrite import EventbriteRepository

    repository = EventbriteRepository(live_app.state.database.sessions, live_app.state.settings)
    account = {"organization_id": ORGANIZATION_ID}
    with pytest.raises(IntegrityError):
        repository.bind_created(
            other_event, account, bound.external_id, bound.bound_by_execution_id
        )
