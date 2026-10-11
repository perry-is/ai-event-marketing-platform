import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.models import (
    Approval,
    AuditEvent,
    CampaignVersion,
    DestinationExecution,
    Event,
    ExecutionAttempt,
)
from app.simulated_connectors import SimulationScenarioController
from tests.conftest import sign_in

DEFAULT_SURFACES = [
    "EVENTBRITE",
    "MEETUP",
    "LINKEDIN_ORGANIZATION",
    "LINKEDIN_PERSONAL",
    "INSTAGRAM",
    "FACEBOOK_GROUPS",
]


def _headers(client, settings):
    return {
        "origin": settings.app_origin,
        "x-csrf-token": client.get("/api/me").json()["csrf_token"],
    }


def _ready_event(client, settings):
    response = client.post(
        "/api/events",
        json={
            "text": (
                "Lunch & Learn on October 22, 2026 from 11:30 AM to 1:00 PM "
                "at Example Venue. Timezone America/Denver."
            )
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 201, response.text
    event = response.json()
    event = client.post(
        f"/api/events/{event['id']}/accept-proposals",
        json={"expected_revision": event["revision"]},
        headers=_headers(client, settings),
    ).json()
    delivery = next(
        item
        for item in event["clarifications"]
        if item["status"] == "OPEN" and item["field_path"] == "delivery"
    )
    response = client.post(
        f"/api/events/{event['id']}/clarifications/{delivery['id']}",
        json={"expected_revision": event["revision"], "answer": "In person"},
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    assert response.json()["readiness"]["ready"]
    return response.json()


def _campaign(client, settings, event, surfaces=None):
    response = client.post(
        f"/api/events/{event['id']}/campaign",
        json={"selected_surfaces": surfaces or DEFAULT_SURFACES},
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approve(client, settings, campaign, *, replacements=None, uncertain=None):
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview")
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["can_approve"] is True
    response = client.post(
        f"/api/campaigns/{campaign['id']}/approve",
        json={
            "expected_revision": data["campaign"]["revision"],
            "campaign_version_id": data["version_id"],
            "version_hash": data["version_hash"],
            "expected_destination_plan_hash": data["destination_plan_hash"],
            "replacement_acknowledgements": replacements or [],
            "uncertain_risk_acknowledgements": uncertain or [],
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _executions(distribution, approval_id):
    return {
        value["destination_key"]: value
        for value in distribution["executions"]
        if value["approval_id"] == approval_id
    }


def _start(client, settings, approval_id):
    return client.post(f"/api/approvals/{approval_id}/start", headers=_headers(client, settings))


def test_approval_and_execution_routes_require_authenticated_session(client):
    identifier = str(uuid.uuid4())
    assert client.get(f"/api/campaigns/{identifier}/distribution").status_code == 401
    assert client.get(f"/api/campaigns/{identifier}/approval-preview").status_code == 401
    assert client.post(f"/api/campaigns/{identifier}/approve", json={}).status_code == 401
    assert client.post(f"/api/approvals/{identifier}/start").status_code == 401
    for action in (
        "retry",
        "reconcile",
        "manual-complete",
        "manual-handoff",
        "acknowledge-failure",
        "acknowledge-uncertain",
        "skip",
    ):
        assert client.post(f"/api/destination-executions/{identifier}/{action}").status_code == 401


def test_exact_version_approval_precedes_and_survives_start_failure(
    client, settings, provider, app, monkeypatch
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE"])
    approval = _approve(client, settings, campaign)
    distribution = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    assert distribution["campaign"]["state"] == "APPROVED"
    assert distribution["active_approval"]["id"] == approval["id"]
    assert distribution["executions"] == []
    assert approval["campaign_version_id"] == campaign["current_version"]["id"]
    assert approval["version_hash"] == campaign["current_version"]["content_hash"]
    assert approval["destination_plan_hash"]
    assert "runtime_mode" not in approval
    assert "runtime_mode" not in approval["destination_plan"]

    def fail_start(*_args, **_kwargs):
        raise RuntimeError("synthetic transaction-B failure")

    monkeypatch.setattr(app.state.execution_service, "start", fail_start)
    failed_start = _start(client, settings, approval["id"])
    assert failed_start.status_code == 500
    retained = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    assert retained["active_approval"]["id"] == approval["id"]
    assert retained["campaign"]["state"] == "APPROVED"
    assert retained["executions"] == []


def test_simulated_execution_is_independent_idempotent_and_manual_is_never_auto(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event)
    approval = _approve(client, settings, campaign)
    started = _start(client, settings, approval["id"])
    assert started.status_code == 200, started.text
    distribution = started.json()
    rows = _executions(distribution, approval["id"])
    assert set(rows) == {
        "EVENTBRITE",
        "MEETUP",
        "LINKEDIN_ORGANIZATION",
        "INSTAGRAM",
        "FACEBOOK_GROUPS",
    }
    for destination in (
        "EVENTBRITE",
        "MEETUP",
        "LINKEDIN_ORGANIZATION",
        "INSTAGRAM",
    ):
        row = rows[destination]
        assert row["status"] == "SUCCEEDED"
        assert row["runtime_mode"] == "SIMULATED"
        assert row["external_id"].startswith("simulated:")
        assert row["external_url"].startswith("https://simulated.invalid/")
        assert len(row["attempts"]) == 1
    personal = next(
        entry
        for entry in approval["destination_plan"]["content_only"]
        if entry["destination_key"] == "LINKEDIN_PERSONAL"
    )
    assert personal["reason"]
    manual = rows["FACEBOOK_GROUPS"]
    assert manual["status"] == "MANUAL_PENDING"
    assert manual["execution_kind"] == "MANUAL"
    assert manual["manual_package"]["approved_copy"]
    assert "No group was selected" in manual["manual_package"]["group_selection"]
    assert manual["manual_package"]["creative_status"] == "No graphic generated"

    repeated = _start(client, settings, approval["id"])
    assert repeated.status_code == 200
    duplicate_rows = _executions(repeated.json(), approval["id"])
    assert all(len(duplicate_rows[key]["attempts"]) == 1 for key in rows)
    assert repeated.json()["summary"]["complete"] is False

    manual_row = duplicate_rows["FACEBOOK_GROUPS"]
    completed = client.post(
        f"/api/destination-executions/{manual_row['id']}/manual-complete",
        json={"note": "Posted to the selected groups manually."},
        headers=_headers(client, settings),
    )
    assert completed.status_code == 200, completed.text
    completed_distribution = completed.json()
    assert completed_distribution["campaign"]["state"] == "COMPLETE"
    assert completed_distribution["summary"]["outcome"] == "COMPLETED_WITH_MANUAL_ACTIONS"
    assert completed_distribution["summary"]["completed_at"]
    with app.state.database.sessions() as db:
        assert (
            db.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "CAMPAIGN_EXECUTION_COMPLETED",
                    AuditEvent.approval_id == uuid.UUID(approval["id"]),
                )
            )
            is not None
        )
    assert (
        _executions(completed_distribution, approval["id"])["FACEBOOK_GROUPS"][
            "verification_status"
        ]
        == "HUMAN_ATTESTED"
    )


def test_retry_targets_only_failed_destination_and_preserves_sibling_successes(
    client, settings, provider, app
):
    sign_in(client, provider)
    app.state.execution_service.scenarios.configure(
        outcomes={"LINKEDIN_ORGANIZATION": ["FAILED_RETRYABLE", "SUCCESS"]}
    )
    event = _ready_event(client, settings)
    campaign = _campaign(
        client,
        settings,
        event,
        ["EVENTBRITE", "MEETUP", "LINKEDIN_ORGANIZATION", "INSTAGRAM"],
    )
    approval = _approve(client, settings, campaign)
    distribution = _start(client, settings, approval["id"]).json()
    failed_rows = _executions(distribution, approval["id"])
    assert failed_rows["LINKEDIN_ORGANIZATION"]["status"] == "FAILED"
    assert failed_rows["LINKEDIN_ORGANIZATION"]["retryable"] is True
    assert all(
        failed_rows[key]["status"] == "SUCCEEDED" for key in ("EVENTBRITE", "MEETUP", "INSTAGRAM")
    )
    before = {key: len(value["attempts"]) for key, value in failed_rows.items()}
    retried = client.post(
        f"/api/destination-executions/{failed_rows['LINKEDIN_ORGANIZATION']['id']}/retry",
        headers=_headers(client, settings),
    )
    assert retried.status_code == 200, retried.text
    after = _executions(retried.json(), approval["id"])
    assert after["LINKEDIN_ORGANIZATION"]["status"] == "SUCCEEDED"
    assert len(after["LINKEDIN_ORGANIZATION"]["attempts"]) == 2
    assert all(
        len(after[key]["attempts"]) == before[key] for key in ("EVENTBRITE", "MEETUP", "INSTAGRAM")
    )
    assert retried.json()["campaign"]["state"] == "COMPLETE"


def test_unknown_blocks_retry_until_reconciliation_or_explicit_risk_accounting(
    client, settings, provider, app
):
    sign_in(client, provider)
    app.state.execution_service.scenarios.configure(
        outcomes={"INSTAGRAM": ["UNKNOWN"]},
        verifications={"INSTAGRAM": ["INCONCLUSIVE"]},
    )
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["INSTAGRAM"])
    approval = _approve(client, settings, campaign)
    distribution = _start(client, settings, approval["id"]).json()
    row = _executions(distribution, approval["id"])["INSTAGRAM"]
    assert row["status"] == "UNKNOWN"
    assert distribution["summary"]["outcome"] == "UNRESOLVED_UNKNOWN"
    retry = client.post(
        f"/api/destination-executions/{row['id']}/retry", headers=_headers(client, settings)
    )
    assert retry.status_code == 409
    reconciled = client.post(
        f"/api/destination-executions/{row['id']}/reconcile",
        headers=_headers(client, settings),
    )
    assert reconciled.status_code == 200
    reconciled_row = _executions(reconciled.json(), approval["id"])["INSTAGRAM"]
    assert reconciled_row["status"] == "UNKNOWN"
    assert len(reconciled_row["attempts"]) == 2
    still_blocked = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    assert still_blocked["summary"]["complete"] is False
    acknowledged = client.post(
        f"/api/destination-executions/{row['id']}/acknowledge-uncertain",
        json={"note": "I understand it may have posted."},
        headers=_headers(client, settings),
    )
    assert acknowledged.status_code == 200
    result = acknowledged.json()
    assert result["campaign"]["state"] == "COMPLETE"
    assert result["summary"]["outcome"] == "COMPLETED_WITH_ACKNOWLEDGED_UNCERTAINTY"
    final_row = _executions(result, approval["id"])["INSTAGRAM"]
    assert final_row["status"] == "UNKNOWN"
    assert final_row["disposition"]["type"] == "ABANDONED_UNCERTAIN"
    assert (
        client.post(
            f"/api/destination-executions/{row['id']}/retry", headers=_headers(client, settings)
        ).status_code
        == 409
    )


def test_unknown_can_reconcile_to_verified_success_or_safe_retry(client, settings, provider, app):
    sign_in(client, provider)
    app.state.execution_service.scenarios.configure(
        outcomes={"INSTAGRAM": ["UNKNOWN"]},
        verifications={"INSTAGRAM": ["FOUND_MATCHING"]},
    )
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["INSTAGRAM"])
    approval = _approve(client, settings, campaign)
    distribution = _start(client, settings, approval["id"]).json()
    row = _executions(distribution, approval["id"])["INSTAGRAM"]
    assert row["status"] == "UNKNOWN"
    reconciled = client.post(
        f"/api/destination-executions/{row['id']}/reconcile",
        headers=_headers(client, settings),
    )
    assert reconciled.status_code == 200
    result = reconciled.json()
    row = _executions(result, approval["id"])["INSTAGRAM"]
    assert row["status"] == "SUCCEEDED"
    assert row["external_id"].startswith("simulated:")
    assert result["summary"]["complete"] is True


def test_final_failure_must_be_explicitly_accounted_for(client, settings, provider, app):
    sign_in(client, provider)
    app.state.execution_service.scenarios.configure(outcomes={"EVENTBRITE": ["FAILED_FINAL"]})
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE"])
    approval = _approve(client, settings, campaign)
    distribution = _start(client, settings, approval["id"]).json()
    row = _executions(distribution, approval["id"])["EVENTBRITE"]
    assert row["status"] == "FAILED"
    assert row["retryable"] is False
    assert distribution["summary"]["complete"] is False
    assert (
        client.post(
            f"/api/destination-executions/{row['id']}/retry", headers=_headers(client, settings)
        ).status_code
        == 409
    )
    acknowledged = client.post(
        f"/api/destination-executions/{row['id']}/acknowledge-failure",
        json={"note": "Do not create this listing."},
        headers=_headers(client, settings),
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["campaign"]["state"] == "COMPLETE"
    assert acknowledged.json()["summary"]["outcome"] == "COMPLETED_WITH_ACKNOWLEDGED_FAILURES"


def test_successor_version_does_not_inherit_prior_exact_version_approval(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["INSTAGRAM"])
    approval = _approve(client, settings, campaign)
    current = client.get(f"/api/campaigns/{campaign['id']}").json()
    revised = client.post(
        f"/api/campaigns/{campaign['id']}/revisions",
        json={
            "expected_revision": current["revision"],
            "revision_request": "Make Instagram warmer.",
        },
        headers=_headers(client, settings),
    )
    assert revised.status_code == 200, revised.text
    successor = revised.json()
    assert successor["current_version_number"] == 2
    status = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    assert status["active_approval"] is None
    assert len(status["approvals"]) == 1
    assert status["approvals"][0]["campaign_version_id"] == approval["campaign_version_id"]
    old_start = _start(client, settings, approval["id"])
    assert old_start.status_code == 409
    assert old_start.json()["error"]["code"] in {
        "APPROVAL_NOT_ACTIVE",
        "APPROVAL_VERSION_NOT_CURRENT",
    }
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    assert preview["version_id"] == successor["current_version"]["id"]
    assert preview["version_id"] != approval["campaign_version_id"]


def test_material_event_change_preserves_but_withdraws_approval_and_notes_do_not(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["INSTAGRAM"])
    approval = _approve(client, settings, campaign)
    with app.state.database.sessions.begin() as db:
        row_event = db.get(Event, uuid.UUID(event["id"]))
        row_event.internal_notes = "Internal note only"
    unchanged = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    assert unchanged["active_approval"]["id"] == approval["id"]
    changed = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": event["revision"],
            "changes": [{"field_path": "title", "value": "Updated Lunch & Learn"}],
        },
        headers=_headers(client, settings),
    )
    assert changed.status_code == 200, changed.text
    result = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    assert result["active_approval"] is None
    assert result["campaign"]["state"] == "DRAFT"
    assert len(result["approvals"]) == 1
    assert result["approvals"][0]["id"] == approval["id"]
    assert result["approvals"][0]["active"] is False
    assert (
        client.post(
            f"/api/approvals/{approval['id']}/start", headers=_headers(client, settings)
        ).status_code
        == 409
    )


def test_material_event_change_is_rejected_while_execution_is_running(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["INSTAGRAM"])
    approval = _approve(client, settings, campaign)
    _start(client, settings, approval["id"])
    with app.state.database.sessions.begin() as db:
        execution = db.scalar(select(DestinationExecution))
        execution.status = "RUNNING"
    blocked = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": event["revision"],
            "changes": [{"field_path": "title", "value": "Moved Event"}],
        },
        headers=_headers(client, settings),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "EVENT_CHANGE_BLOCKED_BY_UNRESOLVED_EXECUTION"


def test_concurrent_and_repeated_start_does_not_duplicate_adapter_attempts(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["INSTAGRAM"])
    approval = _approve(client, settings, campaign)
    with app.state.database.sessions() as db:
        from app.models import AppUser

        actor_id = db.scalar(select(AppUser.id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _index: app.state.execution_service.start(
                    app.state.database.sessions, approval["id"], actor_id
                ),
                range(2),
            )
        )
    assert len(outcomes) == 2
    status = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()
    row = _executions(status, approval["id"])["INSTAGRAM"]
    assert row["status"] == "SUCCEEDED"
    assert len(row["attempts"]) == 1
    assert status["summary"]["complete"] is True


def test_approval_and_final_attempt_are_database_immutable(client, settings, provider, app):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["INSTAGRAM"])
    approval = _approve(client, settings, campaign)
    distribution = _start(client, settings, approval["id"]).json()
    row = _executions(distribution, approval["id"])["INSTAGRAM"]
    with app.state.database.sessions() as db:
        db_approval = db.get(Approval, uuid.UUID(approval["id"]))
        assert db.get(CampaignVersion, uuid.UUID(approval["campaign_version_id"])) is not None
        assert (
            db.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "CAMPAIGN_VERSION_APPROVED",
                    AuditEvent.approval_id == uuid.UUID(approval["id"]),
                )
            )
            is not None
        )
        attempt = db.scalar(
            select(ExecutionAttempt).where(
                ExecutionAttempt.execution_id == uuid.UUID(row["id"]),
                ExecutionAttempt.operation == "PUBLISH",
            )
        )
        with pytest.raises(DBAPIError, match="approvals are immutable"):
            with db.begin_nested():
                db_approval.runtime_mode = "LIVE"
                db.flush()
        with pytest.raises(DBAPIError, match="finalized execution attempts are immutable"):
            with db.begin_nested():
                attempt.outcome = "NOT_CREATED"
                db.flush()


def test_replacement_version_requires_explicit_acknowledgement_of_prior_success(
    client, settings, provider
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["INSTAGRAM"])
    original_approval = _approve(client, settings, campaign)
    started = _start(client, settings, original_approval["id"])
    assert started.status_code == 200, started.text
    original_execution = _executions(started.json(), original_approval["id"])["INSTAGRAM"]
    assert original_execution["status"] == "SUCCEEDED"

    current = client.get(f"/api/campaigns/{campaign['id']}").json()
    revised = client.post(
        f"/api/campaigns/{campaign['id']}/revisions",
        json={
            "expected_revision": current["revision"],
            "revision_request": "Make Instagram warmer.",
        },
        headers=_headers(client, settings),
    )
    assert revised.status_code == 200, revised.text
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    assert "replacement_requirements" in preview, preview
    assert len(preview["replacement_requirements"]) == 1
    requirement = preview["replacement_requirements"][0]
    assert requirement["execution_id"] == original_execution["id"]

    omitted_ack = client.post(
        f"/api/campaigns/{campaign['id']}/approve",
        json={
            "expected_revision": preview["campaign"]["revision"],
            "campaign_version_id": preview["version_id"],
            "version_hash": preview["version_hash"],
            "expected_destination_plan_hash": preview["destination_plan_hash"],
        },
        headers=_headers(client, settings),
    )
    assert omitted_ack.status_code == 409
    assert omitted_ack.json()["error"]["code"] == "REPLACEMENT_CONFIRMATION_REQUIRED"

    replacement = _approve(
        client,
        settings,
        revised.json(),
        replacements=[original_execution["id"]],
    )
    assert replacement["campaign_version_id"] == revised.json()["current_version"]["id"]


def test_test_outcome_control_is_only_mounted_in_test_environment(settings):
    from app.main import create_app

    development = settings.model_copy(update={"environment": "development"})
    app = create_app(development)
    try:
        paths = {getattr(route, "path", None) for route in app.routes}
        assert "/api/test/simulation-scenario" not in paths
        with pytest.raises(RuntimeError, match="require test environment"):
            SimulationScenarioController("production")
    finally:
        app.state.database.engine.dispose()
