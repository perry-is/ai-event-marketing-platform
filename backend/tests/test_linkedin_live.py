import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.config import LINKEDIN_SUPPORTED_API_VERSIONS, Settings
from app.linkedin import POST_ID
from app.models import CampaignVersionAsset, DestinationResourceBinding, GeneratedAsset
from tests.conftest import sign_in
from tests.linkedin_fake import (
    ORGANIZATION_URN,
    FakeLinkedIn,
    FakeLinkedInCredentials,
)
from tests.test_execution import _headers, _ready_event

EVENT_FACTS = {
    "facts.location.venue": "Aurora Event Center",
    "facts.location.address": "123 Main St, Aurora, CO 80012",
    "facts.registration.capacity": "120",
    "facts.registration.price_kind": "FREE",
}


@pytest.fixture
def live_linkedin_app(app, settings):
    settings.linkedin_organization_execution_mode = "LIVE"
    settings.linkedin_api_version = "202609"
    settings.linkedin_client_id = "synthetic-linkedin-client"
    settings.linkedin_client_secret = SecretStr("synthetic-linkedin-client-secret")
    settings.linkedin_redirect_uri = "https://event-marketing.test/auth/linkedin/callback"
    settings.linkedin_organization_id = ORGANIZATION_URN.rsplit(":", 1)[-1]
    settings.linkedin_organization_name = "Event Marketing Page"
    settings.linkedin_community_access_tier = "STANDARD"
    fake = FakeLinkedIn()
    transport = fake.client()
    service = app.state.execution_service
    service.linkedin_http_client = transport
    service.linkedin_credentials = FakeLinkedInCredentials()
    service.rebuild()
    yield app, fake
    transport.close()


@pytest.fixture
def live_linkedin_client(live_linkedin_app):
    app, _fake = live_linkedin_app
    with TestClient(app, base_url="http://localhost:5173", follow_redirects=False) as client:
        yield client


def _setup(client, settings, provider, surfaces=None):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    response = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": event["revision"],
            "changes": [
                {"field_path": path, "value": value} for path, value in EVENT_FACTS.items()
            ],
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/api/events/{event['id']}/campaign",
        json={"selected_surfaces": surfaces or ["LINKEDIN_ORGANIZATION"]},
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
    assert preview["can_approve"], [
        {
            key: item.get(key)
            for key in (
                "destination_key",
                "runtime_mode",
                "readiness",
                "reason",
                "blocker",
                "status",
            )
        }
        for item in preview["destination_readiness"]
    ]
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


def _row(distribution, approval_id):
    return next(
        row
        for row in distribution["executions"]
        if row["approval_id"] == approval_id and row["destination_key"] == "LINKEDIN_ORGANIZATION"
    )


def _linkedin_calls(fake, method=None, path=None):
    return [
        call
        for call in fake.calls
        if call["path"].startswith("/rest/")
        and (method is None or call["method"] == method)
        and (path is None or call["path"] == path)
    ]


def test_current_supported_api_version_and_202510_rejected(settings):
    assert settings.linkedin_api_version == "202609"
    assert "202609" in LINKEDIN_SUPPORTED_API_VERSIONS
    assert "202510" not in LINKEDIN_SUPPORTED_API_VERSIONS
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url=settings.database_url.get_secret_value(),
            linkedin_api_version="202510",
        )


def test_live_readiness_checks_account_page_role_and_never_exposes_secrets(
    live_linkedin_client, live_linkedin_app, provider
):
    app, fake = live_linkedin_app
    sign_in(live_linkedin_client, provider)
    response = live_linkedin_client.get("/api/settings/linkedin-organization")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "READY"
    assert result["checked_live"] is True
    assert result["mode"] == "LIVE"
    assert result["connector_implemented"] is True
    assert result["community_access_tier"] == "STANDARD"
    assert result["organization_name"] == "Event Marketing Page"
    assert "synthetic-linkedin-access-token" not in response.text
    assert "synthetic-linkedin-client-secret" not in response.text
    assert "Authorization" not in response.text
    assert all(call["method"] == "GET" for call in fake.calls)


def test_anonymous_linkedin_settings_and_distribution_are_rejected(client):
    assert client.get("/api/settings/linkedin-organization").status_code == 401
    assert client.get(f"/api/campaigns/{uuid.uuid4()}/approval-preview").status_code == 401


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("linkedin_community_access_tier", "UNVERIFIED", "must be verified"),
        ("linkedin_community_access_tier", "DEVELOPMENT", "Standard Community Management"),
        ("linkedin_organization_execution_mode", "SIMULATED", "Practice mode"),
    ],
)
def test_settings_explain_tier_or_mode_without_claiming_live_readiness(
    live_linkedin_client, live_linkedin_app, provider, field, value, expected
):
    app, _fake = live_linkedin_app
    sign_in(live_linkedin_client, provider)
    setattr(app.state.settings, field, value)
    app.state.execution_service.rebuild()
    result = live_linkedin_client.get("/api/settings/linkedin-organization").json()
    if field == "linkedin_organization_execution_mode":
        assert result["status"] == "READY"
        assert result["mode"] == "SIMULATED"
        assert result["checked_live"] is True
        assert result["reasons"] == []
    else:
        assert result["status"] == "NEEDS_SETUP"
        assert any(expected.lower() in reason.lower() for reason in result["reasons"])
        assert result["checked_live"] is True
        assert result["connected_member"] == "Connected member"


def test_wrong_member_scope_and_page_role_block_readiness(
    live_linkedin_client, live_linkedin_app, settings, provider
):
    app, fake = live_linkedin_app
    sign_in(live_linkedin_client, provider)
    app.state.execution_service.linkedin_credentials = FakeLinkedInCredentials(
        member_urn="urn:li:person:someone-else"
    )
    app.state.execution_service.rebuild()
    data = live_linkedin_client.get("/api/settings/linkedin-organization").json()
    assert data["status"] == "NEEDS_SETUP"
    assert data["reasons"] == ["The connected member cannot publish to this LinkedIn Page"]

    from app.linkedin import REQUIRED_SCOPES

    app.state.execution_service.linkedin_credentials = FakeLinkedInCredentials(
        scopes=REQUIRED_SCOPES - {"w_organization_social"}
    )
    app.state.execution_service.rebuild()
    data = live_linkedin_client.get("/api/settings/linkedin-organization").json()
    assert data["reasons"] == ["LinkedIn is missing a required permission"]

    app.state.execution_service.linkedin_credentials = FakeLinkedInCredentials()
    fake.role = "RECRUITING_POSTER"
    app.state.execution_service.rebuild()
    data = live_linkedin_client.get("/api/settings/linkedin-organization").json()
    assert data["reasons"] == ["The connected member cannot publish to this LinkedIn Page"]


def test_live_plan_freezes_organization_and_campaign_hash_excludes_mode(
    live_linkedin_client, live_linkedin_app, settings, provider
):
    _app, _fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    preview = _preview(live_linkedin_client, campaign["id"])
    entry = preview["destination_plan"]["targets"][0]
    assert entry["runtime_mode"] == "LIVE"
    assert entry["linkedin_organization"]["organization_urn"] == ORGANIZATION_URN
    assert entry["linkedin_organization"]["organization_id"] == "81234567"
    assert "display_name" not in entry["linkedin_organization"]
    initial_hash = preview["version_hash"]
    initial_plan_hash = preview["destination_plan_hash"]

    settings.linkedin_organization_execution_mode = "SIMULATED"
    _app.state.execution_service.rebuild()
    simulated_preview = _preview(live_linkedin_client, campaign["id"])
    assert simulated_preview["version_hash"] == initial_hash
    assert simulated_preview["destination_plan_hash"] != initial_plan_hash
    assert simulated_preview["destination_plan"]["targets"][0]["runtime_mode"] == "SIMULATED"


def test_live_create_uses_posts_api_exact_copy_and_readback_once(
    live_linkedin_client, live_linkedin_app, settings, provider
):
    _app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    preview = _preview(live_linkedin_client, campaign["id"])
    entry = preview["destination_plan"]["targets"][0]
    expected_copy = entry["approved_content"]
    assert "graphic" not in entry
    approval = _approve(live_linkedin_client, settings, campaign["id"])
    distribution = _start(live_linkedin_client, settings, approval["id"])
    result = _row(distribution, approval["id"])
    assert result["status"] == "SUCCEEDED", result
    assert result["external_id"].startswith("urn:li:share:")
    assert result["external_url"] is None
    assert result["result_summary"] == "Published on LinkedIn and verified."
    assert result["verification_status"] == "VERIFIED"
    assert len(fake.posts) == 1
    post = next(iter(fake.posts.values()))
    assert post["commentary"] == expected_copy
    assert post["author"] == ORGANIZATION_URN
    assert post["lifecycleState"] == "PUBLISHED"
    assert post["distribution"]["feedDistribution"] == "MAIN_FEED"
    assert len(_linkedin_calls(fake, "POST", "/rest/posts")) == 1
    assert len(
        _linkedin_calls(fake, "GET", f"/rest/posts/{result['external_id'].replace(':', '%3A')}")
    ) in {0, 1}
    for call in _linkedin_calls(fake):
        assert call["headers"]["linkedin-version"] == "202609"
        assert call["headers"]["x-restli-protocol-version"] == "2.0.0"

    with _app.state.database.sessions() as db:
        binding = db.scalar(
            select(DestinationResourceBinding).where(
                DestinationResourceBinding.destination_key == "LINKEDIN_ORGANIZATION",
                DestinationResourceBinding.event_id == uuid.UUID(campaign["event_id"]),
            )
        )
        assert binding is not None
        assert binding.external_id == result["external_id"]
        assert binding.external_url is None
        assert binding.resource_metadata["organization_urn"] == ORGANIZATION_URN

    _start(live_linkedin_client, settings, approval["id"])
    assert len(_linkedin_calls(fake, "POST", "/rest/posts")) == 1


def test_image_post_uploads_exact_approved_graphic_before_post(
    live_linkedin_client, live_linkedin_app, settings, provider, app
):
    _app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    current = live_linkedin_client.get(f"/api/campaigns/{campaign['id']}").json()
    graphic_version = live_linkedin_client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={"expected_revision": current["revision"]},
        headers=_headers(live_linkedin_client, settings),
    )
    assert graphic_version.status_code == 200, graphic_version.text
    campaign = graphic_version.json()
    graphic = campaign["current_version"]["primary_graphic"]
    approval = _approve(live_linkedin_client, settings, campaign["id"])
    result = _row(_start(live_linkedin_client, settings, approval["id"]), approval["id"])
    assert result["status"] == "SUCCEEDED"
    assert len(fake.uploads) == 1
    assert fake.uploads[0]["authorization"].startswith("Bearer ")
    assert len(fake.posts) == 1
    post = next(iter(fake.posts.values()))
    image_id = post["content"]["media"]["id"]
    assert fake.uploads[0]["image_id"] == image_id
    assert fake.images[image_id]["owner"] == ORGANIZATION_URN
    assert fake.images[image_id]["status"] == "AVAILABLE"
    event_title = live_linkedin_client.get(f"/api/events/{campaign['event_id']}").json()["title"]
    assert post["content"]["media"]["altText"] == event_title
    with app.state.database.sessions() as db:
        asset = db.get(GeneratedAsset, uuid.UUID(graphic["asset_id"]))
        assert asset is not None
        from app.graphics_service import _read_asset

        expected_bytes = _read_asset(settings, asset)
    assert fake.uploads[0]["bytes"] == expected_bytes
    calls = [call for call in fake.calls if call["path"].startswith("/rest/")]
    names = [call["method"] + call["path"] for call in calls]
    assert names.index("POST/rest/images") < next(
        index for index, call in enumerate(fake.calls) if call["method"] == "PUT"
    )
    post_create_index = next(
        index
        for index, call in enumerate(fake.calls)
        if call["method"] == "POST" and call["path"] == "/rest/posts"
    )
    image_read_index = next(
        index
        for index, call in enumerate(fake.calls)
        if call["method"] == "GET" and call["path"].startswith("/rest/images/")
    )
    assert image_read_index < post_create_index


def test_copy_successor_updates_same_post_and_preserves_exact_graphic(
    live_linkedin_client, live_linkedin_app, settings, provider, app
):
    _app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    current = live_linkedin_client.get(f"/api/campaigns/{campaign['id']}").json()
    response = live_linkedin_client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={"expected_revision": current["revision"]},
        headers=_headers(live_linkedin_client, settings),
    )
    campaign = response.json()
    version_a = campaign["current_version"]
    graphic_id = version_a["primary_graphic"]["asset_id"]
    approval_a = _approve(live_linkedin_client, settings, campaign["id"])
    first = _row(_start(live_linkedin_client, settings, approval_a["id"]), approval_a["id"])
    old_copy = fake.posts[first["external_id"]]["commentary"]

    current = live_linkedin_client.get(f"/api/campaigns/{campaign['id']}").json()
    response = live_linkedin_client.post(
        f"/api/campaigns/{campaign['id']}/revisions",
        json={
            "expected_revision": current["revision"],
            "revision_request": "Make LinkedIn warmer.",
        },
        headers=_headers(live_linkedin_client, settings),
    )
    assert response.status_code == 200, response.text
    successor = response.json()
    version_b = successor["current_version"]
    assert version_b["content"]["linkedin_organization_post"] != old_copy
    assert version_b["primary_graphic"]["asset_id"] == graphic_id
    with app.state.database.sessions() as db:
        associations = db.scalars(
            select(CampaignVersionAsset).where(
                CampaignVersionAsset.version_id.in_(
                    [uuid.UUID(version_a["id"]), uuid.UUID(version_b["id"])]
                )
            )
        ).all()
        assert {str(item.asset_id) for item in associations} == {graphic_id}

    approval_b = _approve(live_linkedin_client, settings, campaign["id"])
    second = _row(_start(live_linkedin_client, settings, approval_b["id"]), approval_b["id"])
    assert second["status"] == "SUCCEEDED"
    assert second["external_id"] == first["external_id"]
    assert len(fake.posts) == 1
    assert len(_linkedin_calls(fake, "POST", "/rest/posts")) == 1
    assert len(fake.uploads) == 1
    assert (
        fake.posts[first["external_id"]]["commentary"]
        == version_b["content"]["linkedin_organization_post"]
    )
    assert fake.posts[first["external_id"]]["content"]["media"]["id"] == next(iter(fake.images))
    assert any(call["headers"].get("x-restli-method") == "PARTIAL_UPDATE" for call in fake.calls)


def test_graphic_change_after_published_post_blocks_before_image_or_post_mutation(
    live_linkedin_client, live_linkedin_app, settings, provider
):
    _app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    current = live_linkedin_client.get(f"/api/campaigns/{campaign['id']}").json()
    campaign = live_linkedin_client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={"expected_revision": current["revision"]},
        headers=_headers(live_linkedin_client, settings),
    ).json()
    approval_a = _approve(live_linkedin_client, settings, campaign["id"])
    _start(live_linkedin_client, settings, approval_a["id"])
    original_hash = campaign["current_version"]["primary_graphic"]["sha256"]

    current = live_linkedin_client.get(f"/api/campaigns/{campaign['id']}").json()
    revised = live_linkedin_client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={
            "expected_revision": current["revision"],
            "instruction": "Use less orange.",
        },
        headers=_headers(live_linkedin_client, settings),
    )
    assert revised.status_code == 200, revised.text
    version_b = revised.json()["current_version"]
    assert version_b["primary_graphic"]["sha256"] != original_hash
    preview_b = _preview(live_linkedin_client, campaign["id"])
    linkedin_readiness = next(
        item
        for item in preview_b["destination_readiness"]
        if item["destination_key"] == "LINKEDIN_ORGANIZATION"
    )
    assert linkedin_readiness["status"] == "BLOCKED"
    assert linkedin_readiness["reason"] == "LINKEDIN_MEDIA_REPLACEMENT_UNSUPPORTED"
    approval_b = _approve(live_linkedin_client, settings, campaign["id"])
    before_posts = len(_linkedin_calls(fake, "POST", "/rest/posts"))
    before_uploads = len(fake.uploads)
    distribution = _start(live_linkedin_client, settings, approval_b["id"])
    blocked = _row(distribution, approval_b["id"])
    assert blocked["status"] == "BLOCKED"
    assert "cannot replace media" in blocked["human_error"].lower()
    assert len(_linkedin_calls(fake, "POST", "/rest/posts")) == before_posts
    assert len(fake.uploads) == before_uploads


def test_timeout_after_create_is_unknown_and_never_blindly_retried(
    live_linkedin_client, live_linkedin_app, settings, provider
):
    _app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    approval = _approve(live_linkedin_client, settings, campaign["id"])
    fake.timeout_after_create = True
    distribution = _start(live_linkedin_client, settings, approval["id"])
    row = _row(distribution, approval["id"])
    assert row["status"] == "UNKNOWN"
    assert len(fake.posts) == 1
    assert len(_linkedin_calls(fake, "POST", "/rest/posts")) == 1
    response = live_linkedin_client.post(
        f"/api/destination-executions/{row['id']}/retry",
        headers=_headers(live_linkedin_client, settings),
    )
    assert response.status_code == 409
    reconciled = live_linkedin_client.post(
        f"/api/destination-executions/{row['id']}/reconcile",
        headers=_headers(live_linkedin_client, settings),
    )
    assert reconciled.status_code == 200
    assert _row(reconciled.json(), approval["id"])["status"] == "UNKNOWN"
    assert len(fake.posts) == 1
    assert len(_linkedin_calls(fake, "POST", "/rest/posts")) == 1


def test_known_post_timeout_reconciles_exact_urn_and_copy_update(
    live_linkedin_client, live_linkedin_app, settings, provider
):
    _app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    approval_a = _approve(live_linkedin_client, settings, campaign["id"])
    first = _row(_start(live_linkedin_client, settings, approval_a["id"]), approval_a["id"])
    current = live_linkedin_client.get(f"/api/campaigns/{campaign['id']}").json()
    revised = live_linkedin_client.post(
        f"/api/campaigns/{campaign['id']}/revisions",
        json={
            "expected_revision": current["revision"],
            "revision_request": "Make LinkedIn warmer.",
        },
        headers=_headers(live_linkedin_client, settings),
    ).json()
    approval_b = _approve(live_linkedin_client, settings, campaign["id"])
    fake.timeout_after_update = True
    uncertain = _row(_start(live_linkedin_client, settings, approval_b["id"]), approval_b["id"])
    assert uncertain["status"] == "UNKNOWN"
    assert uncertain["external_id"] == first["external_id"]
    assert len(fake.posts) == 1
    reconciled = live_linkedin_client.post(
        f"/api/destination-executions/{uncertain['id']}/reconcile",
        headers=_headers(live_linkedin_client, settings),
    )
    assert reconciled.status_code == 200
    row = _row(reconciled.json(), approval_b["id"])
    assert row["status"] == "SUCCEEDED"
    assert row["external_id"] == first["external_id"]
    assert (
        fake.posts[first["external_id"]]["commentary"]
        == revised["current_version"]["content"]["linkedin_organization_post"]
    )
    assert len(fake.posts) == 1


def test_retry_uses_same_approved_organization_and_does_not_simulate_config_drift(
    live_linkedin_client, live_linkedin_app, settings, provider
):
    app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    approval = _approve(live_linkedin_client, settings, campaign["id"])
    entry = _preview(live_linkedin_client, campaign["id"])["destination_plan"]["targets"][0]
    assert entry["linkedin_organization"]["organization_urn"] == ORGANIZATION_URN

    settings.linkedin_organization_id = "99999999"
    settings.linkedin_organization_name = "Other Page"
    app.state.execution_service.rebuild()
    result = _start(live_linkedin_client, settings, approval["id"])
    row = _row(result, approval["id"])
    assert row["status"] == "BLOCKED"
    assert "approved LinkedIn Page" in row["human_error"]
    assert len(_linkedin_calls(fake, "POST", "/rest/posts")) == 0
    assert settings.linkedin_organization_execution_mode == "LIVE"


def test_linkedin_client_uses_required_headers_on_versioned_calls(live_linkedin_app, settings):
    _app, fake = live_linkedin_app
    from app.linkedin import LinkedInRestClient

    client = LinkedInRestClient(settings, fake.client())
    try:
        client.organization_roles("synthetic", "urn:li:person:synthetic-member")
    finally:
        client.close()
    call = fake.calls[-1]
    assert call["headers"]["linkedin-version"] == "202609"
    assert call["headers"]["x-restli-protocol-version"] == "2.0.0"
    assert call["url"].find("roleAssignee=urn%3Ali%3Aperson%3Asynthetic-member") >= 0


def test_resource_identity_is_one_linkedin_post_per_local_event_and_frozen_page(
    live_linkedin_client, live_linkedin_app, settings, provider, app
):
    _app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    approval = _approve(live_linkedin_client, settings, campaign["id"])
    result = _row(_start(live_linkedin_client, settings, approval["id"]), approval["id"])
    assert POST_ID.fullmatch(result["external_id"])
    with app.state.database.sessions() as db:
        binding = db.scalar(
            select(DestinationResourceBinding).where(
                DestinationResourceBinding.destination_key == "LINKEDIN_ORGANIZATION",
                DestinationResourceBinding.event_id == uuid.UUID(campaign["event_id"]),
            )
        )
        assert binding.account_key == f"organization:{ORGANIZATION_URN}"
        assert binding.resource_metadata["organization_urn"] == ORGANIZATION_URN
    assert fake.posts[result["external_id"]]["author"] == ORGANIZATION_URN


def test_changed_page_cannot_create_second_linkedin_post_for_same_local_event(
    live_linkedin_client, live_linkedin_app, settings, provider
):
    app, fake = live_linkedin_app
    campaign = _setup(live_linkedin_client, settings, provider)
    approval_a = _approve(live_linkedin_client, settings, campaign["id"])
    first = _row(_start(live_linkedin_client, settings, approval_a["id"]), approval_a["id"])
    assert first["status"] == "SUCCEEDED"

    current = live_linkedin_client.get(f"/api/campaigns/{campaign['id']}").json()
    revised = live_linkedin_client.post(
        f"/api/campaigns/{campaign['id']}/revisions",
        json={
            "expected_revision": current["revision"],
            "revision_request": "Make LinkedIn warmer.",
        },
        headers=_headers(live_linkedin_client, settings),
    )
    assert revised.status_code == 200, revised.text
    approval_b = _approve(live_linkedin_client, settings, campaign["id"])
    settings.linkedin_organization_id = "99999999"
    settings.linkedin_organization_name = "Other Page"
    fake.authorized_org = "urn:li:organization:99999999"
    result = _row(_start(live_linkedin_client, settings, approval_b["id"]), approval_b["id"])
    assert result["status"] == "BLOCKED"
    assert result["error_category"] == "LINKEDIN_ORGANIZATION_CHANGED"
    assert "approved linkedin page" in result["human_error"].lower()
    assert len(fake.posts) == 1
    assert len(_linkedin_calls(fake, "POST", "/rest/posts")) == 1
