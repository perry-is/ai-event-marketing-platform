"""Loopback test harness only. Not installed in the runtime package; no runtime bypass."""

import atexit
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

import httpx2
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select

from app.auth import CurrentSession, require_session
from app.campaign_ai import _expected_claims
from app.campaign_service import CampaignContent, DeterministicCampaignService
from app.config import Settings
from app.group_catalog import import_group_master
from app.main import create_app
from app.models import CredentialEnvelope, OAuthAuthorizationAttempt, ProviderConnection
from app.oauth_connections import OAuthConnectionError, OAuthTokenResponse
from app.oidc import ISSUER, Identity
from tests.database import disposable_database, drop_database, migrate
from tests.eventbrite_fake import ORGANIZATION_ID, ORGANIZER_ID, TOKEN, FakeEventbrite
from tests.instagram_fake import ACCOUNT_ID as INSTAGRAM_ACCOUNT_ID
from tests.instagram_fake import USERNAME as INSTAGRAM_USERNAME
from tests.instagram_fake import FakeInstagram, FakeInstagramCredentials
from tests.linkedin_fake import (
    ORGANIZATION_URN as LINKEDIN_ORGANIZATION_URN,
)
from tests.linkedin_fake import (
    FakeLinkedIn,
    FakeLinkedInCredentials,
)
from tests.meetup_fake import (
    GROUP_URLNAME as MEETUP_GROUP_URLNAME,
)
from tests.meetup_fake import (
    MEMBER_ID as MEETUP_MEMBER_ID,
)
from tests.meetup_fake import (
    NETWORK_URLNAME as MEETUP_NETWORK_URLNAME,
)
from tests.meetup_fake import (
    FakeMeetup,
    write_test_private_key,
)
from tests.openai_test_support import mock_response, text_proposal
from tests.sample_group_master import SAMPLE_MASTER_NAME, sample_group_master

if os.environ.get("EMP_ENVIRONMENT") != "test":
    raise RuntimeError("Browser fixture requires explicit test mode")

url, admin, name = disposable_database()
migrate(url)
settings = Settings(
    _env_file=None,
    environment="test",
    app_origin=os.environ.get("EMP_E2E_APP_ORIGIN", "http://localhost:5174"),
    database_url=url,
    google_client_id="browser-fixture",
    google_client_secret="test-only",
    allowed_emails=["member@example.test"],
    extraction_mode="openai",
    campaign_generation_mode="openai",
    image_generation_mode="fake",
    openai_api_key="test-provider-key",
    openai_campaign_model="gpt-6-luna-test",
    # Eventbrite is configured but starts SIMULATED; its HTTP boundary is the in-memory fake.
    eventbrite_private_token=TOKEN,
    eventbrite_organization_id=ORGANIZATION_ID,
    eventbrite_organizer_id=ORGANIZER_ID,
    eventbrite_currency="USD",
    meetup_client_key="browser-fixture-client",
    meetup_authorized_member_id=MEETUP_MEMBER_ID,
    meetup_signing_key_id="browser-fixture-signing-key",
    meetup_private_key_path=str(
        Path(tempfile.gettempdir()) / f"emp-meetup-test-{uuid.uuid4().hex}.pem"
    ),
    meetup_network_urlname=MEETUP_NETWORK_URLNAME,
    meetup_group_urlname=MEETUP_GROUP_URLNAME,
    linkedin_organization_execution_mode="SIMULATED",
    linkedin_api_version="202609",
    linkedin_client_id="browser-fixture-linkedin-client",
    linkedin_client_secret="browser-fixture-linkedin-secret",
    linkedin_redirect_uri="https://event-marketing.test/auth/linkedin/callback",
    linkedin_organization_id=LINKEDIN_ORGANIZATION_URN.rsplit(":", 1)[-1],
    linkedin_organization_name="Event Marketing Page",
    linkedin_community_access_tier="STANDARD",
    instagram_execution_mode="SIMULATED",
    instagram_graph_api_version="v26.0",
    instagram_professional_account_id=INSTAGRAM_ACCOUNT_ID,
    instagram_username=INSTAGRAM_USERNAME,
    instagram_client_id="browser-fixture-instagram-client",
    instagram_client_secret="browser-fixture-instagram-secret",
    instagram_redirect_uri="https://event-marketing.test/auth/instagram/callback",
    public_media_base_url=(f"http://127.0.0.1:{os.environ.get('EMP_E2E_BACKEND_PORT', '8011')}"),
    media_delivery_signing_secret="browser-fixture-instagram-signing-secret-32-bytes",
)
write_test_private_key(Path(settings.meetup_private_key_path))
fake_eventbrite = FakeEventbrite()
fake_meetup = FakeMeetup()
fake_linkedin = FakeLinkedIn()
fake_instagram = FakeInstagram()


class BrowserFakeOAuthGateway:
    """Browser-only OAuth provider double; it never makes an external request."""

    def __init__(self):
        self.fail_next_exchange = set()

    def authorization_url(self, provider, state, redirect_uri, scopes):
        host = provider.lower()
        return f"https://oauth.{host}.test/authorize?{urlencode({'state': state})}"

    def exchange_code(self, provider, code, redirect_uri, requested_scopes):
        if provider in self.fail_next_exchange:
            self.fail_next_exchange.discard(provider)
            raise OAuthConnectionError("OAUTH_PROVIDER_REJECTED")
        if provider == "LINKEDIN":
            return OAuthTokenResponse(
                access_token="BROWSER_LINKEDIN_ACCESS_SYNTHETIC",
                expires_in=60 * 24 * 60 * 60,
                scopes=frozenset(requested_scopes),
                refresh_token="BROWSER_LINKEDIN_REFRESH_SYNTHETIC",
                refresh_expires_in=365 * 24 * 60 * 60,
            )
        return OAuthTokenResponse(
            access_token="BROWSER_INSTAGRAM_ACCESS_SYNTHETIC",
            expires_in=60 * 24 * 60 * 60,
            scopes=frozenset(requested_scopes),
        )

    def identify(self, provider, access_token):
        if provider == "LINKEDIN":
            return {
                "provider_subject": "browser-member",
                "account_id": "urn:li:person:browser-member",
                "account_name": "Browser Test Member",
            }
        return {
            "provider_subject": INSTAGRAM_ACCOUNT_ID,
            "account_id": INSTAGRAM_ACCOUNT_ID,
            "account_name": INSTAGRAM_USERNAME,
            "account_type": "Business",
        }

    def refresh(self, provider, payload, scopes):
        raise OAuthConnectionError("OAUTH_REAUTH_REQUIRED")

    def revoke(self, provider, payload):
        return True if provider == "LINKEDIN" else None


fake_oauth = BrowserFakeOAuthGateway()


class FixtureOIDC:
    def authorization_url(self, state, nonce, verifier):
        return "/auth/callback?" + urlencode({"state": state, "code": "browser-fixture"})

    async def exchange(self, code, nonce, verifier):
        return Identity(ISSUER, "fixture-subject", "member@example.test", "Test Member")


browser_output = {
    "proposals": [
        text_proposal("title", "Lunch & Learn", "Lunch & Learn"),
        text_proposal(
            "schedule",
            {
                "local_start": "2026-10-22T11:30:00",
                "timezone": "America/Denver",
                "local_end": "2026-10-22T13:00:00",
            },
            "October 22, 2026 from 11:30 AM to 1:00 PM",
        ),
        text_proposal("facts.location.venue", "Example Venue", "at Example Venue"),
    ]
}


def provider_fixture(request):
    if request.url.path != "/v1/responses":
        return httpx2.Response(404, json={"error": {"message": "not found"}})
    body = json.loads(request.content)
    response_name = body["text"]["format"]["name"]
    if response_name == "event_fact_proposals":
        output = browser_output
    elif response_name == "event_marketing_event_description":
        payload = json.loads(body["input"][0]["content"])
        context = payload["event_context"]
        text = f"Join us for {context.get('title') or 'this event'}."
        if context.get("topic"):
            text += f" The event will focus on {context['topic']}."
        if context.get("host"):
            text += f" Hosted by {context['host']}."
        output = {"description": text}
    else:
        prompt = json.loads(body["input"][0]["content"])
        event_facts = prompt["locked_event_facts"]
        surfaces = prompt["selected_surfaces"]
        service = DeterministicCampaignService()
        if prompt.get("revision_request"):
            current = CampaignContent.model_validate(prompt["current_content"], strict=True)
            content, _ = service.revise(current, event_facts, prompt["revision_request"], surfaces)
        else:
            content = service.generate(event_facts, surfaces)
        output = {
            "content": content.model_dump(mode="json"),
            "factual_claims": [
                {"field_path": path, "value_json": value}
                for path, value in _expected_claims(event_facts).items()
            ],
        }
    return mock_response(output, response_id=f"resp_browser_{response_name}")


openai_http_client = httpx2.Client(transport=httpx2.MockTransport(provider_fixture))
app = create_app(
    settings,
    FixtureOIDC(),
    openai_http_client=openai_http_client,
    eventbrite_http_client=fake_eventbrite.client(),
    meetup_http_client=fake_meetup.client(),
    linkedin_http_client=fake_linkedin.client(),
    linkedin_credentials=FakeLinkedInCredentials(),
    instagram_http_client=fake_instagram.client(),
    instagram_credentials=FakeInstagramCredentials(),
    oauth_gateway=fake_oauth,
)
# Keep the WP1I-A no-network double unless a spec explicitly enables the live connector.
app.state.execution_service.live_connector_enabled = False
app.state.execution_service.rebuild()


fake_router = APIRouter(prefix="/api/test/fake-eventbrite", dependencies=[Depends(require_session)])


@fake_router.get("")
def fake_eventbrite_state():
    import hashlib

    return {
        "creates": fake_eventbrite.creates(),
        "updates": sum(
            1
            for method, path in fake_eventbrite.calls
            if method == "POST" and path.startswith("/events/") and path.count("/") == 3
        ),
        "events": [
            {"id": key, "status": value["status"], "name": value["name_text"]}
            for key, value in fake_eventbrite.events.items()
        ],
        "upload_count": len(fake_eventbrite.media),
        "upload_digests": [
            hashlib.sha256(media["bytes"]).hexdigest() for media in fake_eventbrite.media.values()
        ],
        "call_count": len(fake_eventbrite.calls),
    }


@fake_router.post("/timeout-next-create", status_code=204)
def fake_eventbrite_timeout():
    fake_eventbrite.timeout_after_create = True


@fake_router.post("/reset", status_code=204)
def fake_eventbrite_reset():
    fake_eventbrite.reset()


app.include_router(fake_router)

fake_meetup_router = APIRouter(
    prefix="/api/test/fake-meetup", dependencies=[Depends(require_session)]
)


@fake_meetup_router.get("")
def fake_meetup_state():
    return {
        "creates": fake_meetup.created,
        "updates": fake_meetup.updated,
        "events": [
            {"id": item["id"], "title": item["title"], "status": item["status"]}
            for item in fake_meetup.events.values()
        ],
        "calls": fake_meetup.calls,
        "photo_calls": sum(1 for call in fake_meetup.calls if "Photo" in call["operation"]),
        "write_operations": [
            call["operation"]
            for call in fake_meetup.calls
            if call["operation"]
            in {"MeetupEventCreate", "MeetupEventEdit", "MeetupEventPhotoCreate"}
        ],
    }


@fake_meetup_router.post("/reset", status_code=204)
def fake_meetup_reset():
    fake_meetup.reset()


@fake_meetup_router.post("/timeout-next-create", status_code=204)
def fake_meetup_timeout_next_create():
    fake_meetup.timeout_after_create = True


@app.post("/api/test/meetup-live-mode", status_code=204, dependencies=[Depends(require_session)])
def meetup_live_mode():
    if settings.environment != "test":
        from fastapi import HTTPException

        raise HTTPException(404, "NOT_FOUND")
    settings.meetup_execution_mode = "LIVE"
    app.state.execution_service.rebuild()


@app.post(
    "/api/test/meetup-simulated-mode", status_code=204, dependencies=[Depends(require_session)]
)
def meetup_simulated_mode():
    if settings.environment != "test":
        from fastapi import HTTPException

        raise HTTPException(404, "NOT_FOUND")
    settings.meetup_execution_mode = "SIMULATED"
    app.state.execution_service.rebuild()


app.include_router(fake_meetup_router)

fake_linkedin_router = APIRouter(
    prefix="/api/test/fake-linkedin", dependencies=[Depends(require_session)]
)


@fake_linkedin_router.get("")
def fake_linkedin_state():
    import hashlib

    post_writes = [
        call
        for call in fake_linkedin.calls
        if call["path"] == "/rest/posts" and call["method"] == "POST"
    ]
    partial_updates = [
        call
        for call in fake_linkedin.calls
        if call["path"].startswith("/rest/posts/")
        and call["method"] == "POST"
        and call["headers"].get("x-restli-method") == "PARTIAL_UPDATE"
    ]
    return {
        "post_creates": len(post_writes),
        "post_updates": len(partial_updates),
        "posts": [
            {
                "id": row["id"],
                "author": row["author"],
                "commentary": row["commentary"],
                "lifecycleState": row["lifecycleState"],
                "image_id": (row.get("content") or {}).get("media", {}).get("id"),
            }
            for row in fake_linkedin.posts.values()
        ],
        "image_uploads": len(fake_linkedin.uploads),
        "upload_digests": [
            hashlib.sha256(item["bytes"]).hexdigest() for item in fake_linkedin.uploads
        ],
        "call_count": len(fake_linkedin.calls),
        "write_paths": [
            {"method": call["method"], "path": call["path"]}
            for call in fake_linkedin.calls
            if call["method"] != "GET" or call["path"].startswith("/dms-uploads/")
        ],
        "version_headers": sorted(
            {
                call["headers"].get("linkedin-version")
                for call in fake_linkedin.calls
                if call["path"].startswith("/rest/")
            }
        ),
    }


@fake_linkedin_router.post("/reset", status_code=204)
def fake_linkedin_reset():
    fake_linkedin.reset()
    settings.linkedin_organization_id = LINKEDIN_ORGANIZATION_URN.rsplit(":", 1)[-1]
    settings.linkedin_organization_name = "Event Marketing Page"
    app.state.execution_service.rebuild()


@fake_linkedin_router.post("/timeout-next-create", status_code=204)
def fake_linkedin_timeout_next_create():
    fake_linkedin.timeout_after_create = True


@fake_linkedin_router.post("/live-mode", status_code=204)
def linkedin_live_mode():
    settings.linkedin_organization_execution_mode = "LIVE"
    app.state.execution_service.rebuild()


@fake_linkedin_router.post("/simulated-mode", status_code=204)
def linkedin_simulated_mode():
    settings.linkedin_organization_execution_mode = "SIMULATED"
    settings.linkedin_organization_id = LINKEDIN_ORGANIZATION_URN.rsplit(":", 1)[-1]
    settings.linkedin_organization_name = "Event Marketing Page"
    app.state.execution_service.rebuild()


@fake_linkedin_router.post("/other-page", status_code=204)
def linkedin_other_page():
    settings.linkedin_organization_id = "99999999"
    settings.linkedin_organization_name = "Other Page"
    app.state.execution_service.rebuild()


app.include_router(fake_linkedin_router)

fake_instagram_router = APIRouter(
    prefix="/api/test/fake-instagram", dependencies=[Depends(require_session)]
)


@fake_instagram_router.get("")
def fake_instagram_state():
    import hashlib

    return {
        "container_creates": fake_instagram.container_creates,
        "publish_calls": fake_instagram.publish_calls,
        "containers": [
            {
                "id": item["id"],
                "status": item["status_code"],
                "caption": item["caption"],
                "image_sha256": item["image_sha256"],
            }
            for item in fake_instagram.containers.values()
        ],
        "media": list(fake_instagram.media.values()),
        "fetched_image_digests": [
            hashlib.sha256(item["image_bytes"]).hexdigest()
            for item in fake_instagram.containers.values()
        ],
        "write_operations": [call["path"] for call in fake_instagram.write_calls],
    }


@fake_instagram_router.post("/reset", status_code=204)
def fake_instagram_reset():
    fake_instagram.reset()
    settings.instagram_professional_account_id = INSTAGRAM_ACCOUNT_ID
    settings.instagram_username = "eventmarketing_test"
    app.state.execution_service.rebuild()


@fake_instagram_router.post("/live-mode", status_code=204)
def instagram_live_mode():
    settings.instagram_execution_mode = "LIVE"
    app.state.execution_service.rebuild()


@fake_instagram_router.post("/simulated-mode", status_code=204)
def instagram_simulated_mode():
    settings.instagram_execution_mode = "SIMULATED"
    settings.instagram_professional_account_id = INSTAGRAM_ACCOUNT_ID
    settings.instagram_username = INSTAGRAM_USERNAME
    app.state.execution_service.rebuild()


@fake_instagram_router.post("/other-account", status_code=204)
def instagram_other_account():
    settings.instagram_professional_account_id = "17841400000000999"
    app.state.execution_service.rebuild()


@fake_instagram_router.post("/timeout-next-publish", status_code=204)
def instagram_timeout_next_publish():
    fake_instagram.timeout_after_publish = True


app.include_router(fake_instagram_router)

fake_oauth_router = APIRouter(
    prefix="/api/test/fake-oauth", dependencies=[Depends(require_session)]
)
CurrentBrowserSession = Annotated[CurrentSession, Depends(require_session)]


def _oauth_provider(value):
    provider = {"linkedin": "LINKEDIN", "instagram": "INSTAGRAM"}.get(value)
    if provider is None:
        raise HTTPException(404, "NOT_FOUND")
    return provider


@fake_oauth_router.get("/{provider}")
def fake_oauth_state(provider: str, current: CurrentBrowserSession):
    provider_key = _oauth_provider(provider)
    metadata = app.state.oauth_connections.metadata(provider, current.user_id)
    token = (
        "BROWSER_LINKEDIN_ACCESS_SYNTHETIC"
        if provider_key == "LINKEDIN"
        else "BROWSER_INSTAGRAM_ACCESS_SYNTHETIC"
    )
    refresh = "BROWSER_LINKEDIN_REFRESH_SYNTHETIC" if provider_key == "LINKEDIN" else ""
    with app.state.database.sessions() as db:
        connection = db.scalar(
            select(ProviderConnection).where(ProviderConnection.provider == provider_key)
        )
        envelope = (
            db.scalar(
                select(CredentialEnvelope).where(CredentialEnvelope.connection_id == connection.id)
            )
            if connection
            else None
        )
        raw_values = []
        if connection:
            raw_values.extend(
                [
                    connection.provider,
                    connection.status,
                    connection.account_id,
                    connection.account_name,
                ]
            )
        if envelope:
            raw_values.extend([envelope.key_id, envelope.nonce.hex(), envelope.ciphertext.hex()])
        database_plaintext_leak = any(
            value and value in " ".join(str(item) for item in raw_values if item)
            for value in (token, refresh)
        )
        envelope_digest = hashlib.sha256(envelope.ciphertext).hexdigest() if envelope else None
    try:
        if envelope is None or connection.status != "CONNECTED":
            raise OAuthConnectionError("OAUTH_CREDENTIAL_UNAVAILABLE")
        credentials = (
            app.state.execution_service.linkedin_credentials
            if provider_key == "LINKEDIN"
            else app.state.execution_service.instagram_credentials
        )
        grant = credentials.access_grant()
        provider_credential_usable = bool(grant.connected and grant.access_token == token)
    except Exception:
        provider_credential_usable = False
    return {
        "status": metadata["status"],
        "account_name": metadata["account_name"],
        "account_type": metadata["account_type"],
        "granted_scopes": metadata["granted_scopes"],
        "envelope_present": envelope is not None,
        "envelope_digest": envelope_digest,
        "database_plaintext_leak": database_plaintext_leak,
        "provider_credential_usable": provider_credential_usable,
    }


@fake_oauth_router.post("/{provider}/fail-next-exchange", status_code=204)
def fake_oauth_fail_next_exchange(provider: str):
    fake_oauth.fail_next_exchange.add(_oauth_provider(provider))


@fake_oauth_router.post("/{provider}/reset", status_code=204)
def fake_oauth_reset(provider: str):
    provider_key = _oauth_provider(provider)
    fake_oauth.fail_next_exchange.discard(provider_key)
    with app.state.database.sessions.begin() as db:
        db.execute(
            delete(OAuthAuthorizationAttempt).where(
                OAuthAuthorizationAttempt.provider == provider_key
            )
        )
        db.execute(delete(ProviderConnection).where(ProviderConnection.provider == provider_key))


app.include_router(fake_oauth_router)
# Import the synthetic Group Master into the disposable database.
import_group_master(
    app.state.database.sessions,
    settings.private_storage_dir,
    sample_group_master(),
    SAMPLE_MASTER_NAME,
)


@atexit.register
def cleanup():
    app.state.database.engine.dispose()
    app.state.extractor.close()
    app.state.campaign_service.close()
    Path(settings.meetup_private_key_path).unlink(missing_ok=True)
    drop_database(admin, name)


class _ObservedLoginLimiter:
    """Harness-only observer: same limits as production, but records rejections in the log."""

    def __init__(self, inner):
        self.inner = inner

    def check(self, request):
        try:
            self.inner.check(request)
        except Exception:
            import logging

            logging.getLogger("event_marketing").warning("test_login_rate_limited")
            raise


app.state.login_limiter = _ObservedLoginLimiter(app.state.login_limiter)
