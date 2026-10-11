import hashlib
import uuid
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.config import Settings
from app.graphics_service import _read_asset
from app.instagram import (
    MEDIA_URL_TTL_SECONDS,
    _decode_signed_token,
    _safe_jpeg,
    create_signed_media_url,
    signed_media_bytes,
    static_blockers,
)
from app.models import DestinationResourceBinding, GeneratedAsset
from tests.conftest import sign_in
from tests.instagram_fake import (
    ACCESS_TOKEN,
    ACCOUNT_ID,
    USERNAME,
    FakeInstagram,
    FakeInstagramCredentials,
)
from tests.test_execution import _headers, _ready_event

SIGNING_SECRET = "synthetic-instagram-signing-secret-with-more-than-32-bytes"


@pytest.fixture
def live_instagram_app(app, settings):
    settings.instagram_execution_mode = "LIVE"
    settings.instagram_graph_api_version = "v26.0"
    settings.instagram_professional_account_id = ACCOUNT_ID
    settings.instagram_username = USERNAME
    settings.public_media_base_url = "https://media.example.test"
    settings.media_delivery_signing_secret = SecretStr(SIGNING_SECRET)

    def fetch_media(url):
        token = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        return signed_media_bytes(app.state.database.sessions, settings, token)

    fake = FakeInstagram(media_fetcher=fetch_media)
    transport = fake.client()
    service = app.state.execution_service
    service.instagram_http_client = transport
    service.instagram_credentials = FakeInstagramCredentials()
    service.rebuild()
    yield app, fake
    transport.close()


@pytest.fixture
def live_instagram_client(live_instagram_app):
    app, _fake = live_instagram_app
    with TestClient(app, base_url="http://localhost:5173", follow_redirects=False) as client:
        yield client


def _setup(client, settings, provider):
    sign_in(client, provider)
    event = _ready_event(client, settings)
    response = client.post(
        f"/api/events/{event['id']}/campaign",
        json={"selected_surfaces": ["INSTAGRAM"]},
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _add_graphic(client, settings, campaign):
    current = client.get(f"/api/campaigns/{campaign['id']}").json()
    response = client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={"expected_revision": current["revision"]},
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


def _instagram_row(distribution, approval_id):
    return next(
        item
        for item in distribution["executions"]
        if item["approval_id"] == approval_id and item["destination_key"] == "INSTAGRAM"
    )


def test_instagram_settings_are_truthful_and_do_not_expose_credentials(
    live_instagram_client, live_instagram_app, provider
):
    app, fake = live_instagram_app
    sign_in(live_instagram_client, provider)
    response = live_instagram_client.get("/api/settings/instagram")
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "LIVE"
    assert result["status"] == "READY"
    assert result["checked_live"] is True
    assert result["account_eligible"] is True
    assert result["username"] == USERNAME
    assert all(call["method"] == "GET" for call in fake.calls)
    assert ACCESS_TOKEN not in response.text
    assert SIGNING_SECRET not in response.text
    assert "Authorization" not in response.text


def test_anonymous_instagram_status_and_execution_are_rejected(client):
    assert client.get("/api/settings/instagram").status_code == 401
    assert client.post("/api/approvals/not-an-approval/start").status_code == 401


@pytest.mark.parametrize(
    ("profile", "scopes", "expected"),
    [
        ({"account_type": "Personal"}, None, "A Business or Creator Instagram account is required"),
        (
            {"user_id": "17841400000000999"},
            None,
            "Connected Instagram account does not match the configured account",
        ),
        ({}, {"instagram_business_basic"}, "Instagram publishing permission is missing"),
    ],
)
def test_readiness_requires_professional_account_and_both_scopes(
    live_instagram_client, live_instagram_app, settings, provider, profile, scopes, expected
):
    app, fake = live_instagram_app
    sign_in(live_instagram_client, provider)
    fake.profile_override = profile
    if scopes is not None:
        app.state.execution_service.instagram_credentials = FakeInstagramCredentials(scopes=scopes)
        app.state.execution_service.rebuild()
    result = live_instagram_client.get("/api/settings/instagram").json()
    assert result["status"] == "NEEDS_SETUP"
    assert expected in result["reasons"]
    assert not fake.write_calls


@pytest.mark.parametrize(
    ("graph_error", "expected"),
    [
        (190, "Instagram rejected the connection"),
        (10, "The connected Instagram account lacks permission"),
        (4, "Instagram asked us to slow down; try again later"),
    ],
)
def test_graph_api_errors_are_normalized_without_provider_detail(
    live_instagram_client, live_instagram_app, provider, graph_error, expected
):
    _app, fake = live_instagram_app
    sign_in(live_instagram_client, provider)
    fake.graph_error_next = graph_error
    response = live_instagram_client.get("/api/settings/instagram")
    assert response.status_code == 200
    assert expected in response.json()["reasons"]
    assert "fake" not in response.text
    assert "synthetic-instagram-access-token" not in response.text
    assert not fake.write_calls


def test_missing_live_credential_provider_blocks_without_meta_calls(
    live_instagram_client, live_instagram_app, provider
):
    app, fake = live_instagram_app
    sign_in(live_instagram_client, provider)
    service = app.state.execution_service
    service.instagram_credentials = None
    service.rebuild()
    before = len(fake.calls)
    result = live_instagram_client.get("/api/settings/instagram").json()
    assert result["status"] == "NEEDS_SETUP"
    assert "Instagram account has not connected this app" in result["reasons"]
    assert len(fake.calls) == before


def test_live_plan_freezes_account_and_version_hash_is_independent_of_mode(
    live_instagram_client, live_instagram_app, settings, provider
):
    app, _fake = live_instagram_app
    campaign = _setup(live_instagram_client, settings, provider)
    campaign = _add_graphic(live_instagram_client, settings, campaign)
    first = _preview(live_instagram_client, campaign["id"])
    entry = first["destination_plan"]["targets"][0]
    assert entry["runtime_mode"] == "LIVE"
    assert entry["instagram_account"]["account_id"] == ACCOUNT_ID
    assert entry["instagram_account"]["username"] == USERNAME
    content_hash = first["version_hash"]
    plan_hash = first["destination_plan_hash"]
    settings.instagram_execution_mode = "SIMULATED"
    app.state.execution_service.rebuild()
    second = _preview(live_instagram_client, campaign["id"])
    assert second["version_hash"] == content_hash
    assert second["destination_plan_hash"] != plan_hash
    assert second["destination_plan"]["targets"][0]["runtime_mode"] == "SIMULATED"


def test_live_mode_needs_public_https_media_and_signing_configuration(settings):
    settings.instagram_execution_mode = "LIVE"
    settings.environment = "development"
    settings.instagram_professional_account_id = ACCOUNT_ID
    settings.public_media_base_url = "http://localhost:8011"
    settings.media_delivery_signing_secret = SecretStr(SIGNING_SECRET)
    blockers = static_blockers(settings, FakeInstagramCredentials())
    assert "INSTAGRAM_MEDIA_DELIVERY_REQUIRES_HTTPS" in blockers


def test_live_image_publish_fetches_exact_signed_asset_and_binds_verified_post(
    live_instagram_client, live_instagram_app, settings, provider, app
):
    _app, fake = live_instagram_app
    campaign = _add_graphic(
        live_instagram_client,
        settings,
        _setup(live_instagram_client, settings, provider),
    )
    preview = _preview(live_instagram_client, campaign["id"])
    entry = preview["destination_plan"]["targets"][0]
    approved_caption = entry["approved_content"]
    approved_graphic = entry["approved_graphic"]
    approval = _approve(live_instagram_client, settings, campaign["id"])
    distribution = live_instagram_client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(live_instagram_client, settings)
    )
    assert distribution.status_code == 200, distribution.text
    row = _instagram_row(distribution.json(), approval["id"])
    assert row["status"] == "SUCCEEDED", row
    assert row["external_id"] in fake.media
    assert row["external_url"] == fake.media[row["external_id"]]["permalink"]
    assert row["result_summary"] == "Published on Instagram and verified."
    assert fake.container_creates == 1
    assert fake.publish_calls == 1
    assert fake.containers["fake-container-0001"]["caption"] == approved_caption
    with app.state.database.sessions() as db:
        asset = db.get(GeneratedAsset, uuid.UUID(approved_graphic["asset_id"]))
        original = _read_asset(settings, asset)
    expected_jpeg, _width, _height = _safe_jpeg(original, approved_graphic["sha256"])
    assert (
        fake.containers["fake-container-0001"]["image_sha256"]
        == hashlib.sha256(expected_jpeg).hexdigest()
    )
    assert fake.media[row["external_id"]]["caption"] == approved_caption
    assert fake.media[row["external_id"]]["media_type"] == "IMAGE"
    assert all(
        call["headers"].get("authorization") == f"Bearer {ACCESS_TOKEN}" for call in fake.calls
    )
    assert all(call["path"].startswith("/v26.0/") for call in fake.calls)
    with app.state.database.sessions() as db:
        binding = db.scalar(
            select(DestinationResourceBinding).where(
                DestinationResourceBinding.destination_key == "INSTAGRAM",
                DestinationResourceBinding.event_id == uuid.UUID(campaign["event_id"]),
            )
        )
        assert binding is not None
        assert binding.external_id == row["external_id"]
        assert binding.external_url == row["external_url"]
        assert binding.account_key == f"professional-account:{ACCOUNT_ID}"
        assert binding.resource_metadata["graphic_sha256"] == approved_graphic["sha256"]
    repeat = live_instagram_client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(live_instagram_client, settings)
    )
    assert repeat.status_code == 200
    assert fake.container_creates == 1
    assert fake.publish_calls == 1


def test_provider_container_processing_error_does_not_publish(
    live_instagram_client, live_instagram_app, settings, provider
):
    _app, fake = live_instagram_app
    fake.next_container_status = "ERROR"
    campaign = _add_graphic(
        live_instagram_client,
        settings,
        _setup(live_instagram_client, settings, provider),
    )
    approval = _approve(live_instagram_client, settings, campaign["id"])
    result = live_instagram_client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(live_instagram_client, settings)
    ).json()
    row = _instagram_row(result, approval["id"])
    assert row["status"] == "FAILED"
    assert row["error_category"] == "MEDIA_PROCESSING"
    assert fake.container_creates == 1
    assert fake.publish_calls == 0


def test_unresolved_container_processing_is_bounded_and_never_publishes(
    live_instagram_client, live_instagram_app, settings, provider
):
    app, fake = live_instagram_app
    fake.next_container_status = "IN_PROGRESS"
    campaign = _add_graphic(
        live_instagram_client,
        settings,
        _setup(live_instagram_client, settings, provider),
    )
    preview = _preview(live_instagram_client, campaign["id"])
    entry = next(
        item
        for item in preview["destination_plan"]["targets"]
        if item["destination_key"] == "INSTAGRAM"
    )
    adapter = app.state.execution_service.adapters.for_entry(entry)
    adapter.client.sleeper = lambda _seconds: None
    approval = _approve(live_instagram_client, settings, campaign["id"])
    result = live_instagram_client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(live_instagram_client, settings)
    ).json()
    row = _instagram_row(result, approval["id"])
    assert row["status"] == "UNKNOWN"
    container_reads = sum(
        call["method"] == "GET" and "fake-container" in call["path"] for call in fake.calls
    )
    assert container_reads == 5
    assert fake.publish_calls == 0


def test_uncertain_container_create_is_not_repeated(
    live_instagram_client, live_instagram_app, settings, provider
):
    _app, fake = live_instagram_app
    campaign = _add_graphic(
        live_instagram_client,
        settings,
        _setup(live_instagram_client, settings, provider),
    )
    approval = _approve(live_instagram_client, settings, campaign["id"])
    fake.timeout_after_create = True
    result = live_instagram_client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(live_instagram_client, settings)
    ).json()
    row = _instagram_row(result, approval["id"])
    assert row["status"] == "UNKNOWN"
    assert fake.container_creates == 1
    assert fake.publish_calls == 0
    retry = live_instagram_client.post(
        f"/api/destination-executions/{row['id']}/retry",
        headers=_headers(live_instagram_client, settings),
    )
    assert retry.status_code == 409
    assert fake.container_creates == 1


def test_no_graphic_blocks_approval_before_any_instagram_write(
    live_instagram_client, live_instagram_app, settings, provider
):
    _app, fake = live_instagram_app
    campaign = _setup(live_instagram_client, settings, provider)
    preview = _preview(live_instagram_client, campaign["id"])
    assert preview["can_approve"] is False
    readiness = next(
        item for item in preview["destination_readiness"] if item["destination_key"] == "INSTAGRAM"
    )
    assert readiness["status"] == "BLOCKED"
    assert readiness["reason"] == "INSTAGRAM_GRAPHIC_REQUIRED"
    assert not fake.write_calls


def test_published_instagram_resource_blocks_successor_without_mutation(
    live_instagram_client, live_instagram_app, settings, provider
):
    app, fake = live_instagram_app
    campaign = _add_graphic(
        live_instagram_client,
        settings,
        _setup(live_instagram_client, settings, provider),
    )
    approval = _approve(live_instagram_client, settings, campaign["id"])
    started = live_instagram_client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(live_instagram_client, settings)
    ).json()
    assert _instagram_row(started, approval["id"])["status"] == "SUCCEEDED"
    before = (fake.container_creates, fake.publish_calls)
    current = live_instagram_client.get(f"/api/campaigns/{campaign['id']}").json()
    revised = live_instagram_client.post(
        f"/api/campaigns/{campaign['id']}/revisions",
        json={
            "expected_revision": current["revision"],
            "revision_request": "Make Instagram warmer.",
        },
        headers=_headers(live_instagram_client, settings),
    )
    assert revised.status_code == 200, revised.text
    next_approval = _approve(live_instagram_client, settings, campaign["id"])
    result = live_instagram_client.post(
        f"/api/approvals/{next_approval['id']}/start",
        headers=_headers(live_instagram_client, settings),
    ).json()
    row = _instagram_row(result, next_approval["id"])
    assert row["status"] == "BLOCKED"
    assert "already published" in row["human_error"].lower()
    assert (fake.container_creates, fake.publish_calls) == before
    assert app.state.database.engine.dialect.name == "postgresql"


def test_publish_timeout_becomes_unknown_and_cannot_be_retried_blindly(
    live_instagram_client, live_instagram_app, settings, provider
):
    _app, fake = live_instagram_app
    campaign = _add_graphic(
        live_instagram_client,
        settings,
        _setup(live_instagram_client, settings, provider),
    )
    approval = _approve(live_instagram_client, settings, campaign["id"])
    fake.timeout_after_publish = True
    started = live_instagram_client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(live_instagram_client, settings)
    ).json()
    row = _instagram_row(started, approval["id"])
    assert row["status"] == "UNKNOWN"
    assert fake.publish_calls == 1
    response = live_instagram_client.post(
        f"/api/destination-executions/{row['id']}/retry",
        headers=_headers(live_instagram_client, settings),
    )
    assert response.status_code == 409
    assert fake.container_creates == 1
    assert fake.publish_calls == 1
    reconciled = live_instagram_client.post(
        f"/api/destination-executions/{row['id']}/reconcile",
        headers=_headers(live_instagram_client, settings),
    )
    assert reconciled.status_code == 200
    assert _instagram_row(reconciled.json(), approval["id"])["status"] == "UNKNOWN"
    assert fake.container_creates == 1
    assert fake.publish_calls == 1


def test_old_approval_never_redirects_to_changed_account(
    live_instagram_client, live_instagram_app, settings, provider
):
    app, fake = live_instagram_app
    campaign = _add_graphic(
        live_instagram_client,
        settings,
        _setup(live_instagram_client, settings, provider),
    )
    approval = _approve(live_instagram_client, settings, campaign["id"])
    settings.instagram_professional_account_id = "17841400000000999"
    app.state.execution_service.rebuild()
    started = live_instagram_client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(live_instagram_client, settings)
    ).json()
    row = _instagram_row(started, approval["id"])
    assert row["status"] == "BLOCKED"
    assert not fake.write_calls


def test_signed_media_endpoint_is_public_asset_scoped_and_expiring(
    live_instagram_client, live_instagram_app, settings, provider
):
    app, _fake = live_instagram_app
    campaign = _add_graphic(
        live_instagram_client,
        settings,
        _setup(live_instagram_client, settings, provider),
    )
    version = campaign["current_version"]
    graphic = version["primary_graphic"]
    signed = create_signed_media_url(settings, version_id=version["id"], graphic=graphic, now=1_000)
    token = urlsplit(signed).path.rsplit("/", 1)[-1]
    expiry_claim = _decode_signed_token(settings, token, now=1_001)
    assert expiry_claim["exp"] - expiry_claim["iat"] == MEDIA_URL_TTL_SECONDS

    anonymous = TestClient(app, base_url="http://localhost:5173", follow_redirects=False)
    fetched = anonymous.get(urlsplit(signed).path)
    assert fetched.status_code == 404  # synthetic token is already expired at current time

    live_url = create_signed_media_url(settings, version_id=version["id"], graphic=graphic)
    live_token = urlsplit(live_url).path.rsplit("/", 1)[-1]
    anonymous_response = anonymous.get(urlsplit(live_url).path)
    assert anonymous_response.status_code == 200
    assert anonymous_response.headers["content-type"] == "image/jpeg"
    assert anonymous_response.headers["cache-control"] == "no-store"
    assert anonymous_response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in anonymous_response.headers
    with app.state.database.sessions() as db:
        asset = db.get(GeneratedAsset, uuid.UUID(graphic["asset_id"]))
        original = _read_asset(settings, asset)
    expected, _width, _height = _safe_jpeg(original, graphic["sha256"])
    assert anonymous_response.content == expected
    head = anonymous.head(urlsplit(live_url).path)
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(expected))

    tampered = live_token[:-1] + ("A" if live_token[-1] != "A" else "B")
    assert anonymous.get(f"/api/public/instagram-media/{tampered}").status_code == 404
    assert anonymous.post(f"/api/public/instagram-media/{live_token}").status_code == 405
    assert anonymous.get("/api/public/instagram-media/../../etc/passwd").status_code in {404, 405}
    other_asset = {**graphic, "asset_id": str(uuid.uuid4())}
    unassociated = create_signed_media_url(settings, version_id=version["id"], graphic=other_asset)
    assert anonymous.get(urlsplit(unassociated).path).status_code == 404
    assert signed_media_bytes(app.state.database.sessions, settings, live_token) == expected


def test_settings_fail_closed_for_missing_signing_key_and_invalid_version(settings):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="test",
            database_url=settings.database_url.get_secret_value(),
            instagram_graph_api_version="v2025",
        )
