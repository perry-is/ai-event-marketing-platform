"""System-integration audit coverage (docs/system-integration-audit.md).

These tests exercise the real application wiring end to end over no-network provider fakes:
real OAuth-to-vault credentials (no credential fallbacks), the real adapter registry built by
``create_app`` and the real Approval/execution services. Regression cases assert the repaired
dynamic readiness, BLOCKED resolution, expired execution lease, and approval-preview binding.
"""

import hashlib
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.credential_vault import InMemoryCredentialKeyProvider
from app.instagram import signed_media_bytes
from app.main import create_app
from tests.conftest import sign_in
from tests.eventbrite_fake import FakeEventbrite, live_settings_values
from tests.instagram_fake import ACCESS_TOKEN as INSTAGRAM_TOKEN
from tests.instagram_fake import ACCOUNT_ID, USERNAME, FakeInstagram
from tests.linkedin_fake import ORGANIZATION_URN, FakeLinkedIn
from tests.test_execution import _headers, _ready_event
from tests.test_facebook_groups import _import_master
from tests.test_oauth_vault import FakeOAuthGateway, _begin, _callback

AURORA = {
    "facts.location.venue": "Aurora Event Center",
    "facts.location.address": "123 Main St, Aurora, CO 80012",
    "facts.registration.capacity": "120",
    "facts.registration.price_kind": "FREE",
}
SURFACES = ["EVENTBRITE", "MEETUP", "LINKEDIN_ORGANIZATION", "INSTAGRAM", "FACEBOOK_GROUPS"]
VAULT_KEY = b"A" * 32


class FakeProviderTokenGateway(FakeOAuthGateway):
    """OAuth double whose issued tokens are the ones the provider fakes accept."""

    def exchange_code(self, provider, code, redirect_uri, requested_scopes):
        response = super().exchange_code(provider, code, redirect_uri, requested_scopes)
        if provider == "INSTAGRAM":
            return type(response)(
                access_token=INSTAGRAM_TOKEN,
                expires_in=response.expires_in,
                scopes=response.scopes,
            )
        return response


class Stack:
    """One application process wired like production, over shared fakes and one database."""

    def __init__(self, settings, provider):
        values = settings.model_dump() | live_settings_values()
        values |= {
            "meetup_execution_mode": "SIMULATED",
            "linkedin_organization_execution_mode": "LIVE",
            "linkedin_api_version": "202609",
            "linkedin_client_id": "synthetic-linkedin-client",
            "linkedin_client_secret": SecretStr("synthetic-linkedin-client-secret"),
            "linkedin_redirect_uri": "https://event-marketing.test/auth/linkedin/callback",
            "linkedin_organization_id": ORGANIZATION_URN.rsplit(":", 1)[-1],
            "linkedin_organization_name": "Event Marketing Page",
            "linkedin_community_access_tier": "STANDARD",
            "instagram_execution_mode": "LIVE",
            "instagram_graph_api_version": "v26.0",
            "instagram_professional_account_id": ACCOUNT_ID,
            "instagram_username": USERNAME,
            "instagram_client_id": "synthetic-instagram-client",
            "instagram_client_secret": SecretStr("synthetic-instagram-client-secret"),
            "instagram_redirect_uri": "https://event-marketing.test/auth/instagram/callback",
            "public_media_base_url": "https://media.example.test",
            "media_delivery_signing_secret": SecretStr("s" * 48),
            "credential_vault_active_key_id": "audit-key",
        }
        self.settings = Settings(_env_file=None, **values)
        self.provider = provider
        self.eventbrite = FakeEventbrite()
        self.linkedin = FakeLinkedIn()
        self.instagram = FakeInstagram(media_fetcher=self._fetch_media)
        self.gateway = FakeProviderTokenGateway()
        self.key_provider = InMemoryCredentialKeyProvider({"audit-key": VAULT_KEY}, "audit-key")
        self.app = None

    def _fetch_media(self, url):
        token = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        return signed_media_bytes(self.app.state.database.sessions, self.settings, token)

    def start_process(self):
        """Create a fresh application process (equivalent to a server restart)."""
        self.app = create_app(
            self.settings,
            self.provider.oidc,
            eventbrite_http_client=self.eventbrite.client(),
            linkedin_http_client=self.linkedin.client(),
            instagram_http_client=self.instagram.client(),
            oauth_gateway=self.gateway,
            credential_key_provider=self.key_provider,
        )
        return TestClient(self.app, base_url="http://localhost:5173", follow_redirects=False)


@pytest.fixture
def stack(app, settings, provider):
    # ``app`` resets the shared test database before this independent stack is built.
    stack = Stack(settings, provider)
    stack.base_app = app
    return stack


def _post(client, settings, path, body=None):
    return client.post(path, json=body or {}, headers=_headers(client, settings))


def _connect(client, settings, slug):
    response = _callback(client, slug, _begin(client, settings, slug))
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/settings?connection={slug}_connected"


def _readiness(client, campaign_id):
    preview = client.get(f"/api/campaigns/{campaign_id}/approval-preview").json()
    return preview, {item["destination_key"]: item for item in preview["destination_readiness"]}


def _ready_campaign(client, stack):
    settings = stack.settings
    sign_in(client, stack.provider)
    # Messy text intake -> proposals accepted -> delivery clarification answered -> ready.
    event = _ready_event(client, settings)
    response = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": event["revision"],
            "changes": [{"field_path": key, "value": value} for key, value in AURORA.items()],
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    assert response.json()["readiness"]["ready"]
    response = _post(
        client, settings, f"/api/events/{event['id']}/campaign", {"selected_surfaces": SURFACES}
    )
    assert response.status_code == 200, response.text
    return event, response.json()


def test_cross_connector_end_to_end_mixed_workflow_with_vault_credentials(stack):
    _import_master(stack.base_app, stack.settings)
    with stack.start_process() as client:
        settings = stack.settings
        event, campaign = _ready_campaign(client, stack)
        campaign_id = campaign["id"]

        # Settings OAuth stores encrypted credentials in the vault (no fallback providers).
        _connect(client, settings, "linkedin")
        _connect(client, settings, "instagram")
        connections = stack.app.state.oauth_connections
        assert connections.has_active_credentials("LINKEDIN")
        assert connections.has_active_credentials("INSTAGRAM")

    # HIGH finding #1: readiness is captured when the registry is built, so the connections
    # above only take effect in a new process. Restart, as an operator would have to.
    with stack.start_process() as client:
        sign_in(client, stack.provider)
        current = client.get(f"/api/campaigns/{campaign_id}").json()
        response = _post(
            client,
            settings,
            f"/api/campaigns/{campaign_id}/graphics",
            {"expected_revision": current["revision"]},
        )
        assert response.status_code == 200, response.text

        # Human Facebook Group selection, frozen into a successor CampaignVersion.
        eligible = client.get(f"/api/campaigns/{campaign_id}/eligible-groups").json()["groups"]
        chosen = [group for group in eligible if group["selectable"]][:2]
        assert chosen, "the Group Master should offer eligible groups for Aurora, CO"
        for group in chosen:
            revision = client.get(f"/api/campaigns/{campaign_id}").json()["revision"]
            response = _post(
                client,
                settings,
                f"/api/campaigns/{campaign_id}/group-decisions",
                {
                    "expected_revision": revision,
                    "group_record_id": group["id"],
                    "decision": "SELECT",
                },
            )
            assert response.status_code == 200, response.text
        preview, _ = _readiness(client, campaign_id)
        assert preview["group_selection_pending"] is True and preview["can_approve"] is False
        revision = client.get(f"/api/campaigns/{campaign_id}").json()["revision"]
        response = _post(
            client,
            settings,
            f"/api/campaigns/{campaign_id}/group-selection",
            {"expected_revision": revision},
        )
        assert response.status_code == 200, response.text

        preview, readiness = _readiness(client, campaign_id)
        assert preview["can_approve"], readiness
        modes = {
            target["destination_key"]: target.get("runtime_mode") or target["execution_kind"]
            for target in preview["destination_plan"]["targets"]
        }
        assert modes == {
            "EVENTBRITE": "LIVE",
            "MEETUP": "SIMULATED",
            "LINKEDIN_ORGANIZATION": "LIVE",
            "INSTAGRAM": "LIVE",
            "FACEBOOK_GROUPS": "MANUAL",
        }
        response = _post(
            client,
            settings,
            f"/api/campaigns/{campaign_id}/approve",
            {
                "expected_revision": preview["campaign"]["revision"],
                "campaign_version_id": preview["version_id"],
                "version_hash": preview["version_hash"],
                "expected_destination_plan_hash": preview["destination_plan_hash"],
                "replacement_acknowledgements": [],
                "uncertain_risk_acknowledgements": [],
            },
        )
        assert response.status_code == 200, response.text
        approval = response.json()
        assert approval["destination_plan_hash"] == preview["destination_plan_hash"]

    # Restart between Approval and Start: the stored Approval alone authorizes execution.
    with stack.start_process() as client:
        sign_in(client, stack.provider)
        response = _post(client, settings, f"/api/approvals/{approval['id']}/start")
        assert response.status_code == 200, response.text
        result = response.json()
        rows = {
            row["destination_key"]: row
            for row in result["executions"]
            if row["approval_id"] == approval["id"]
        }
        assert {key: rows[key]["status"] for key in rows} == {
            "EVENTBRITE": "SUCCEEDED",
            "MEETUP": "SUCCEEDED",
            "LINKEDIN_ORGANIZATION": "SUCCEEDED",
            "INSTAGRAM": "SUCCEEDED",
            "FACEBOOK_GROUPS": "MANUAL_PENDING",
        }
        for key in ("EVENTBRITE", "LINKEDIN_ORGANIZATION", "INSTAGRAM"):
            assert rows[key]["runtime_mode"] == "LIVE"
            assert rows[key]["verification_status"] == "VERIFIED", key
        assert rows["MEETUP"]["verification_status"] == "SIMULATED"
        assert result["summary"]["complete"] is False

        # Every provider received exactly the approved version's graphic (one write each).
        graphic = approval["destination_plan"]["targets"][0]["approved_graphic"]
        assert all(
            target.get("approved_graphic", {}).get("sha256") == graphic["sha256"]
            for target in approval["destination_plan"]["targets"]
        )
        assert [
            hashlib.sha256(item["bytes"]).hexdigest() for item in stack.eventbrite.media.values()
        ] == [graphic["sha256"]]
        assert [hashlib.sha256(item["bytes"]).hexdigest() for item in stack.linkedin.uploads] == [
            graphic["sha256"]
        ]
        assert (stack.instagram.container_creates, stack.instagram.publish_calls) == (1, 1)
        assert stack.eventbrite.creates() == 1

        package = rows["FACEBOOK_GROUPS"]["manual_package"]
        assert {group["group_record_id"] for group in package["selected_groups"]} == {
            group["id"] for group in chosen
        }
        assert package["graphic_sha256"] == graphic["sha256"]

        response = _post(
            client,
            settings,
            f"/api/destination-executions/{rows['FACEBOOK_GROUPS']['id']}/manual-complete",
            {"note": "Posted in both approved groups."},
        )
        assert response.status_code == 200, response.text
        summary = response.json()["summary"]
        assert summary["complete"] is True
        assert summary["outcome"] == "COMPLETED_WITH_MANUAL_ACTIONS"

        # A second Start is idempotent: no duplicate provider writes.
        response = _post(client, settings, f"/api/approvals/{approval['id']}/start")
        assert response.status_code == 200, response.text
        assert stack.eventbrite.creates() == 1
        assert len(stack.linkedin.uploads) == 1
        assert (stack.instagram.container_creates, stack.instagram.publish_calls) == (1, 1)


def test_oauth_connection_makes_live_destinations_approvable_without_restart(stack):
    with stack.start_process() as client:
        _, campaign = _ready_campaign(client, stack)
        # Instagram LIVE requires an approved image; keep that independent prerequisite
        # satisfied while proving that account readiness changes dynamically.
        current = client.get(f"/api/campaigns/{campaign['id']}").json()
        response = _post(
            client,
            stack.settings,
            f"/api/campaigns/{campaign['id']}/graphics",
            {"expected_revision": current["revision"]},
        )
        assert response.status_code == 200, response.text
        _, before = _readiness(client, campaign["id"])
        assert before["LINKEDIN_ORGANIZATION"]["reason"] == "LINKEDIN_ACCOUNT_NOT_CONNECTED"
        assert before["INSTAGRAM"]["reason"] == "INSTAGRAM_ACCOUNT_NOT_CONNECTED"
        _connect(client, stack.settings, "linkedin")
        _connect(client, stack.settings, "instagram")
        _, after = _readiness(client, campaign["id"])
        assert after["LINKEDIN_ORGANIZATION"]["status"] == "READY", after
        assert after["INSTAGRAM"]["status"] == "READY", after
        preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
        assert preview["can_approve"]
        approved = _post(
            client,
            stack.settings,
            f"/api/campaigns/{campaign['id']}/approve",
            {
                "expected_revision": preview["campaign"]["revision"],
                "campaign_version_id": preview["version_id"],
                "version_hash": preview["version_hash"],
                "expected_destination_plan_hash": preview["destination_plan_hash"],
                "replacement_acknowledgements": [],
                "uncertain_risk_acknowledgements": [],
            },
        )
        assert approved.status_code == 200, approved.text
        # Disconnect through the same application services without restarting or
        # rebuilding the static connector registry.
        for provider in ("linkedin", "instagram"):
            response = _post(client, stack.settings, f"/api/oauth/{provider}/disconnect")
            assert response.status_code == 200, response.text
        _, disconnected = _readiness(client, campaign["id"])
        assert disconnected["LINKEDIN_ORGANIZATION"]["reason"] == "LINKEDIN_ACCOUNT_NOT_CONNECTED"
        assert disconnected["INSTAGRAM"]["reason"] == "INSTAGRAM_ACCOUNT_NOT_CONNECTED"
        execution = _post(client, stack.settings, f"/api/approvals/{approved.json()['id']}/start")
        assert execution.status_code == 200, execution.text
        rows = {
            row["destination_key"]: row
            for row in execution.json()["executions"]
            if row["destination_key"] in {"LINKEDIN_ORGANIZATION", "INSTAGRAM"}
        }
        for destination in ("LINKEDIN_ORGANIZATION", "INSTAGRAM"):
            assert rows[destination]["status"] == "BLOCKED"
            assert rows[destination]["runtime_mode"] == "LIVE"


def test_provider_blocked_successor_is_previewed_and_can_be_accounted_for(stack):
    """Known Instagram immutability is previewed and has the existing safe resolution."""
    stack.settings.eventbrite_execution_mode = "SIMULATED"
    with stack.start_process() as client:
        sign_in(client, stack.provider)
        _connect(client, stack.settings, "linkedin")
        _connect(client, stack.settings, "instagram")
    with stack.start_process() as client:
        settings = stack.settings
        _, campaign = _ready_campaign(client, stack)
        campaign_id = campaign["id"]
        current = client.get(f"/api/campaigns/{campaign_id}").json()
        _post(
            client,
            settings,
            f"/api/campaigns/{campaign_id}/graphics",
            {"expected_revision": current["revision"]},
        )

        def approve_and_start(*, expect_immutable_block=False):
            preview, readiness = _readiness(client, campaign_id)
            assert preview["can_approve"], readiness
            if expect_immutable_block:
                assert readiness["INSTAGRAM"]["status"] == "BLOCKED"
                assert readiness["INSTAGRAM"]["reason"] == "INSTAGRAM_ALREADY_PUBLISHED_IMMUTABLE"
            else:
                assert readiness["INSTAGRAM"]["status"] == "READY"
            approval = _post(
                client,
                settings,
                f"/api/campaigns/{campaign_id}/approve",
                {
                    "expected_revision": preview["campaign"]["revision"],
                    "campaign_version_id": preview["version_id"],
                    "version_hash": preview["version_hash"],
                    "expected_destination_plan_hash": preview["destination_plan_hash"],
                    "replacement_acknowledgements": [
                        item["execution_id"] for item in preview["replacement_requirements"]
                    ],
                    "uncertain_risk_acknowledgements": [],
                },
            ).json()
            started = _post(client, settings, f"/api/approvals/{approval['id']}/start").json()
            rows = {
                row["destination_key"]: row
                for row in started["executions"]
                if row["approval_id"] == approval["id"]
            }
            return started, rows

        _started, rows = approve_and_start()
        assert rows["INSTAGRAM"]["status"] == "SUCCEEDED"
        for row in rows.values():
            if row["status"] == "MANUAL_PENDING":
                _post(client, settings, f"/api/destination-executions/{row['id']}/manual-complete")

        current = client.get(f"/api/campaigns/{campaign_id}").json()
        response = _post(
            client,
            settings,
            f"/api/campaigns/{campaign_id}/revisions",
            {
                "expected_revision": current["revision"],
                "revision_request": "Make Instagram warmer.",
            },
        )
        assert response.status_code == 200, response.text
        started, rows = approve_and_start(expect_immutable_block=True)
        for row in rows.values():
            if row["status"] == "MANUAL_PENDING":
                started = _post(
                    client, settings, f"/api/destination-executions/{row['id']}/manual-complete"
                ).json()
        instagram = rows["INSTAGRAM"]
        assert instagram["status"] == "BLOCKED"
        assert "already published" in instagram["human_error"].lower()
        assert stack.instagram.publish_calls == 1
        assert started["summary"]["outcome"] == "ACTION_REQUIRED"
        assert started["summary"]["complete"] is False
        retry = _post(client, settings, f"/api/destination-executions/{instagram['id']}/retry")
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] == "DESTINATION_NOT_RETRYABLE"

        resolved = _post(
            client,
            settings,
            f"/api/destination-executions/{instagram['id']}/acknowledge-failure",
            {"note": "Instagram post already published; leave it."},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["summary"]["complete"] is True
        assert resolved.json()["summary"]["outcome"] == "COMPLETED_WITH_ACKNOWLEDGED_FAILURES"


def test_expired_orphaned_running_execution_recovers_to_unknown_without_retry(
    app, client, settings, provider, monkeypatch
):
    """Expired execution ownership becomes UNKNOWN; no provider write is repeated."""
    from datetime import timedelta

    import app.execution_service as execution_service
    from app.execution_service import _claim_auto_attempt, initialize_execution
    from tests.test_execution import _approve, _campaign

    sign_in(client, provider)
    event = _ready_event(client, settings)
    campaign = _campaign(client, settings, event, ["EVENTBRITE"])
    approval = _approve(client, settings, campaign)
    service = app.state.execution_service
    sessions = app.state.database.sessions
    with sessions() as db:
        from sqlalchemy import select

        from app.models import AppUser

        actor = db.scalar(select(AppUser.id))
    [execution_id] = initialize_execution(sessions, approval["id"], actor, service.adapters)
    assert _claim_auto_attempt(
        sessions, execution_id, actor, adapters=service.adapters, retry=False
    )
    with sessions() as db:
        from sqlalchemy import select

        from app.models import ExecutionAttempt

        attempt = db.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.execution_id == execution_id)
        )
        assert attempt.status == "RUNNING"
        lease_expires_at = attempt.lease_expires_at
    before_expiry = client.get(f"/api/campaigns/{campaign['id']}/distribution")
    assert before_expiry.status_code == 200
    running = next(row for row in before_expiry.json()["executions"] if row["id"] == execution_id)
    assert running["status"] == "RUNNING"
    resumed = _post(client, settings, f"/api/approvals/{approval['id']}/start")
    assert resumed.status_code == 200
    still_running = next(row for row in resumed.json()["executions"] if row["id"] == execution_id)
    assert still_running["status"] == "RUNNING"
    assert [item["operation"] for item in still_running["attempts"]] == ["PUBLISH"]

    monkeypatch.setattr(execution_service, "_now", lambda: lease_expires_at + timedelta(seconds=1))
    # Two independent recovery callers race over the same expired lease. The row lock and
    # recheck allow exactly one to finalize it.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovered_counts = list(
            pool.map(
                lambda _index: execution_service.recover_expired_execution_attempts(sessions),
                range(2),
            )
        )
    assert sum(recovered_counts) == 1
    distribution = client.get(f"/api/campaigns/{campaign['id']}/distribution")
    row = next(value for value in distribution.json()["executions"] if value["id"] == execution_id)
    assert row["status"] == "UNKNOWN"
    assert row["retryable"] is False
    assert row["attempts"][0]["status"] == "FINALIZED"
    assert row["attempts"][0]["outcome"] == "UNCERTAIN"
    assert row["attempts"][0]["evidence"]["recovery"] == "EXECUTION_INTERRUPTED"
    retry = _post(client, settings, f"/api/destination-executions/{execution_id}/retry")
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "DESTINATION_NOT_RETRYABLE"
    assert [attempt["operation"] for attempt in row["attempts"]] == ["PUBLISH"]
    resolved = _post(
        client,
        settings,
        f"/api/destination-executions/{execution_id}/acknowledge-uncertain",
        {"note": "I accounted for the unresolved provider result."},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["summary"]["complete"] is True
    assert resolved.json()["summary"]["outcome"] == "COMPLETED_WITH_ACKNOWLEDGED_UNCERTAINTY"
