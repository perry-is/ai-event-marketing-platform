import importlib

from app.execution_service import destination_plan_hash
from app.models import Approval, CampaignVersion
from app.simulated_connectors import DestinationAdapterRegistry
from tests.conftest import sign_in
from tests.test_execution import _approve, _campaign, _headers, _ready_event, _start


def _set_modes(app, settings, **modes):
    for name, value in modes.items():
        setattr(settings, f"{name}_execution_mode", value)
    app.state.execution_service.adapters = DestinationAdapterRegistry(
        settings, app.state.execution_service.scenarios
    )


def test_destination_plan_hash_is_stable_and_excludes_transient_values():
    base = {
        "schema_version": 2,
        "targets": [
            {
                "destination_key": "EVENTBRITE",
                "target_key": "account-main",
                "execution_kind": "AUTO",
                "runtime_mode": "SIMULATED",
                "approved_content": "Exact approved copy",
                "ui_label": "Event Marketing Organization",
                "readiness": "READY",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "destination_key": "FACEBOOK_GROUPS",
                "target_key": "manual-package",
                "execution_kind": "MANUAL",
                "selected_groups": [
                    {"group_record_id": "group-b", "url": "https://facebook.test/b"},
                    {"group_record_id": "group-a", "url": "https://facebook.test/a"},
                ],
                "approved_content": "Exact group copy",
            },
        ],
        "exclusions": [],
        "content_only": [],
    }
    equivalent = {
        **base,
        "targets": [
            {
                **base["targets"][1],
                "selected_groups": list(reversed(base["targets"][1]["selected_groups"])),
            },
            {
                **base["targets"][0],
                "created_at": "2030-01-01T00:00:00Z",
                "readiness": "BLOCKED",
                "ui_label": "A different display label",
            },
        ],
    }
    assert destination_plan_hash(base) == destination_plan_hash(equivalent)

    changed_mode = {
        **base,
        "targets": [{**base["targets"][0], "runtime_mode": "LIVE"}, base["targets"][1]],
    }
    changed_targets = {
        **base,
        "targets": [
            base["targets"][0],
            {**base["targets"][1], "selected_groups": [{"group_record_id": "group-c"}]},
        ],
    }
    assert destination_plan_hash(changed_mode) != destination_plan_hash(base)
    assert destination_plan_hash(changed_targets) != destination_plan_hash(base)


def test_migration_normalizes_historical_simulated_approval_without_changing_meaning():
    migration = importlib.import_module("migrations.versions.0007_mixed_mode_approval")
    legacy = {
        "schema_version": 1,
        "runtime_mode": "SIMULATED",
        "targets": [
            {
                "destination_key": "EVENTBRITE",
                "target_key": "simulated-default",
                "execution_kind": "AUTO",
                "runtime_mode": "SIMULATED",
                "campaign_version_id": "version-pk",
            },
            {
                "destination_key": "FACEBOOK_GROUPS",
                "target_key": "manual-package",
                "execution_kind": "MANUAL",
                "runtime_mode": "SIMULATED",
                "selected_groups": [],
            },
        ],
        "exclusions": [],
        "content_only": [],
    }
    normalized = migration._normalize_legacy_plan(legacy)
    assert normalized["schema_version"] == 2
    assert "runtime_mode" not in normalized
    assert normalized["targets"][0]["runtime_mode"] == "SIMULATED"
    assert "runtime_mode" not in normalized["targets"][1]
    assert migration._plan_hash(normalized) == destination_plan_hash(normalized)


def test_server_derived_mixed_plan_approval_and_execution(client, settings, provider, app):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event)
    version_hash_before = campaign["current_version"]["content_hash"]

    _set_modes(app, settings, eventbrite="LIVE")
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    entries = {entry["destination_key"]: entry for entry in preview["destination_plan"]["targets"]}
    assert entries["EVENTBRITE"]["runtime_mode"] == "LIVE"
    for destination in ("MEETUP", "LINKEDIN_ORGANIZATION", "INSTAGRAM"):
        assert entries[destination]["runtime_mode"] == "SIMULATED"
    assert entries["FACEBOOK_GROUPS"]["execution_kind"] == "MANUAL"
    assert "runtime_mode" not in entries["FACEBOOK_GROUPS"]
    assert preview["can_approve"] is True
    assert preview["version_hash"] == version_hash_before

    approval = _approve(client, settings, campaign)
    assert approval["version_hash"] == version_hash_before
    assert approval["destination_plan_hash"] == destination_plan_hash(approval["destination_plan"])
    assert "runtime_mode" not in approval
    with app.state.database.sessions() as db:
        stored_approval = db.get(Approval, approval["id"])
        stored_version = db.get(CampaignVersion, approval["campaign_version_id"])
        assert stored_approval.approved_plan_hash is None
        assert stored_approval.runtime_mode is None
        assert stored_version.content_hash == version_hash_before

    started = _start(client, settings, approval["id"])
    assert started.status_code == 200, started.text
    rows = {
        row["destination_key"]: row
        for row in started.json()["executions"]
        if row["approval_id"] == approval["id"]
    }
    assert rows["EVENTBRITE"]["runtime_mode"] == "LIVE"
    assert rows["EVENTBRITE"]["status"] == "SUCCEEDED"
    assert rows["EVENTBRITE"]["verification_status"] == "TEST_MOCKED"
    assert rows["EVENTBRITE"]["external_id"].startswith("test-live:eventbrite:")
    assert "no-network" in rows["EVENTBRITE"]["result_summary"].lower()
    assert rows["MEETUP"]["runtime_mode"] == "SIMULATED"
    assert rows["MEETUP"]["external_id"].startswith("simulated:")
    assert rows["FACEBOOK_GROUPS"]["execution_kind"] == "MANUAL"
    assert rows["FACEBOOK_GROUPS"]["runtime_mode"] is None
    assert rows["FACEBOOK_GROUPS"]["status"] == "MANUAL_PENDING"
    assert len(rows) == 5


def test_browser_cannot_submit_runtime_mode_or_destination_plan_hash(client, settings, provider):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE"])
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    base = {
        "expected_revision": preview["campaign"]["revision"],
        "campaign_version_id": preview["version_id"],
        "version_hash": preview["version_hash"],
        "expected_destination_plan_hash": preview["destination_plan_hash"],
    }
    for forged in (
        {"runtime_mode": "LIVE"},
        {"destination_plan_hash": "0" * 64},
    ):
        response = client.post(
            f"/api/campaigns/{campaign['id']}/approve",
            json=base | forged,
            headers=_headers(client, settings),
        )
        assert response.status_code == 422


def test_approval_requires_the_exact_preview_plan_hash(client, settings, provider, app):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE"])
    preview_a = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    assert preview_a["can_approve"]
    original_hash = preview_a["destination_plan_hash"]

    _set_modes(app, settings, eventbrite="LIVE")
    changed = client.post(
        f"/api/campaigns/{campaign['id']}/approve",
        json={
            "expected_revision": preview_a["campaign"]["revision"],
            "campaign_version_id": preview_a["version_id"],
            "version_hash": preview_a["version_hash"],
            "expected_destination_plan_hash": original_hash,
            "replacement_acknowledgements": [],
            "uncertain_risk_acknowledgements": [],
        },
        headers=_headers(client, settings),
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "APPROVAL_PLAN_CHANGED"
    with app.state.database.sessions() as db:
        assert db.query(Approval).filter(Approval.campaign_id == campaign["id"]).count() == 0

    _set_modes(app, settings, eventbrite="SIMULATED")
    preview_again = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    assert preview_again["destination_plan_hash"] == original_hash
    approved = _approve(client, settings, campaign)
    assert approved["destination_plan_hash"] == original_hash


def test_preview_plan_includes_safe_server_derived_target_labels(client, settings, provider, app):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE", "FACEBOOK_GROUPS"])
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    entries = {row["destination_key"]: row for row in preview["destination_plan"]["targets"]}
    assert entries["EVENTBRITE"]["ui_label"] == "Event Marketing Organization"
    assert entries["FACEBOOK_GROUPS"]["ui_label"].endswith("selected Groups")


def test_unsupported_live_mode_is_shown_as_blocked_before_approval(client, settings, provider, app):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["MEETUP"])
    _set_modes(app, settings, meetup="LIVE")
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    assert preview["can_approve"] is False
    assert preview["destination_readiness"][0]["runtime_mode"] == "LIVE"
    assert preview["destination_readiness"][0]["status"] == "BLOCKED"
    assert preview["destination_readiness"][0]["reason"] == "APPROVED_CONNECTOR_MODE_UNAVAILABLE"


def test_live_approval_blocks_when_server_is_reconfigured_to_simulated(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE", "MEETUP"])
    _set_modes(app, settings, eventbrite="LIVE")
    approval = _approve(client, settings, campaign)

    _set_modes(app, settings, eventbrite="SIMULATED")
    blocked = _start(client, settings, approval["id"])
    assert blocked.status_code == 200, blocked.text
    eventbrite = next(
        row
        for row in blocked.json()["executions"]
        if row["approval_id"] == approval["id"] and row["destination_key"] == "EVENTBRITE"
    )
    meetup = next(
        row
        for row in blocked.json()["executions"]
        if row["approval_id"] == approval["id"] and row["destination_key"] == "MEETUP"
    )
    assert eventbrite["status"] == "BLOCKED"
    assert eventbrite["error_category"] == "APPROVED_CONNECTOR_MODE_UNAVAILABLE"
    assert eventbrite["attempts"] == []
    assert eventbrite["external_id"] is None
    assert meetup["status"] == "SUCCEEDED"

    _set_modes(app, settings, eventbrite="LIVE")
    resumed = _start(client, settings, approval["id"])
    eventbrite_after = next(
        row
        for row in resumed.json()["executions"]
        if row["approval_id"] == approval["id"] and row["destination_key"] == "EVENTBRITE"
    )
    assert eventbrite_after["status"] == "SUCCEEDED"
    assert eventbrite_after["runtime_mode"] == "LIVE"
    assert eventbrite_after["external_id"].startswith("test-live:eventbrite:")


def test_changed_server_plan_requires_a_new_approval_and_preserves_the_old_plan(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE"])
    _set_modes(app, settings, eventbrite="LIVE")
    first = _approve(client, settings, campaign)
    first_hash = first["destination_plan_hash"]

    _set_modes(app, settings, eventbrite="SIMULATED")
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    assert preview["can_approve"] is True
    assert preview["destination_plan_hash"] != first_hash
    second = _approve(client, settings, campaign)
    assert second["id"] != first["id"]
    assert second["destination_plan_hash"] == preview["destination_plan_hash"]
    assert second["destination_plan"]["targets"][0]["runtime_mode"] == "SIMULATED"

    history = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()["approvals"]
    by_id = {approval["id"]: approval for approval in history}
    assert by_id[first["id"]]["destination_plan_hash"] == first_hash
    assert by_id[first["id"]]["destination_plan"]["targets"][0]["runtime_mode"] == "LIVE"
    assert by_id[first["id"]]["active"] is False
    assert by_id[second["id"]]["active"] is True


def test_returning_to_a_previously_approved_plan_requires_a_fresh_approval(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE"])

    _set_modes(app, settings, eventbrite="LIVE")
    first = _approve(client, settings, campaign)
    _set_modes(app, settings, eventbrite="SIMULATED")
    _approve(client, settings, campaign)
    _set_modes(app, settings, eventbrite="LIVE")
    third = _approve(client, settings, campaign)

    assert third["destination_plan_hash"] == first["destination_plan_hash"]
    assert third["id"] != first["id"]
    history = client.get(f"/api/campaigns/{campaign['id']}/distribution").json()["approvals"]
    assert (
        sum(item["destination_plan_hash"] == first["destination_plan_hash"] for item in history)
        == 2
    )
    assert next(item for item in history if item["id"] == first["id"])["active"] is False
    assert next(item for item in history if item["id"] == third["id"])["active"] is True


def test_simulated_approval_retries_in_simulated_mode_after_live_configuration_change(
    client, settings, provider, app
):
    sign_in(client, provider)
    app.state.execution_service.scenarios.configure(outcomes={"EVENTBRITE": ["FAILED_RETRYABLE"]})
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE"])
    approval = _approve(client, settings, campaign)
    started = _start(client, settings, approval["id"])
    row = next(
        row for row in started.json()["executions"] if row["destination_key"] == "EVENTBRITE"
    )
    execution_id = row["id"]
    assert row["runtime_mode"] == "SIMULATED"
    assert row["status"] == "FAILED" and row["retryable"] is True

    _set_modes(app, settings, eventbrite="LIVE")
    retried = client.post(
        f"/api/destination-executions/{execution_id}/retry",
        headers=_headers(client, settings),
    )
    assert retried.status_code == 200, retried.text
    retry_row = next(row for row in retried.json()["executions"] if row["id"] == execution_id)
    assert retry_row["runtime_mode"] == "SIMULATED"
    assert retry_row["status"] == "SUCCEEDED"
    assert retry_row["external_id"].startswith("simulated:eventbrite:")


def test_historical_simulated_approval_and_manual_package_migrate_round_trip(
    client, settings, provider, app
):
    from alembic import command
    from sqlalchemy import create_engine, text

    from tests.database import migrate

    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event)
    approval = _approve(client, settings, campaign)
    result = _start(client, settings, approval["id"])
    assert result.status_code == 200, result.text

    url = settings.database_url.get_secret_value()
    config = migrate(url)
    command.downgrade(config, "0006_facebook_groups")
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as db:
            stored_plan = db.execute(text("SELECT destination_plan FROM approvals")).scalar_one()
            manual = db.execute(
                text(
                    "SELECT runtime_mode, plan_entry FROM destination_executions "
                    "WHERE execution_kind = 'MANUAL'"
                )
            ).one()
            assert stored_plan["schema_version"] == 2
            assert "runtime_mode" not in stored_plan
            assert all(
                target["runtime_mode"] == "SIMULATED"
                for target in stored_plan["targets"]
                if target["execution_kind"] == "AUTO"
            )
            assert manual.runtime_mode is None
            assert "runtime_mode" not in manual.plan_entry
    finally:
        engine.dispose()
