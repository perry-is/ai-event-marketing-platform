import base64
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.credential_vault import (
    CredentialVault,
    CredentialVaultError,
    FileCredentialKeyProvider,
    InMemoryCredentialKeyProvider,
)
from app.instagram import REQUIRED_SCOPES as INSTAGRAM_SCOPES
from app.instagram import InstagramAPIClient, InstagramError
from app.linkedin import (
    REQUIRED_SCOPES as LINKEDIN_SCOPES,
)
from app.linkedin import (
    LinkedInError,
    LinkedInRestClient,
)
from app.models import (
    CredentialEnvelope,
    OAuthAuthorizationAttempt,
    ProviderConnection,
)
from app.oauth_connections import OAuthConnectionError, OAuthTokenResponse
from tests.conftest import sign_in
from tests.instagram_fake import ACCOUNT_ID, USERNAME
from tests.linkedin_fake import MEMBER_URN
from tests.test_execution import _headers

LINKEDIN_ACCESS = "LINKEDIN_ACCESS_SUPER_SECRET_TEST_VALUE"
LINKEDIN_REFRESH = "LINKEDIN_REFRESH_SUPER_SECRET_TEST_VALUE"
INSTAGRAM_ACCESS = "INSTAGRAM_ACCESS_SUPER_SECRET_TEST_VALUE"
KEY_OLD = b"O" * 32
KEY_NEW = b"N" * 32


class FakeOAuthGateway:
    def __init__(self):
        self.fail_exchange = False
        self.exchange_calls = []
        self.refresh_calls = []
        self.revoke_result = True
        self.expiry_seconds = 60 * 24 * 60 * 60
        self.include_linkedin_refresh = False
        self.scope_override = None
        self.account_override = None
        self.refresh_delay = 0
        self.refresh_fail = False
        self._lock = threading.Lock()

    def authorization_url(self, provider, state, redirect_uri, scopes):
        self.authorization = (provider, state, redirect_uri, frozenset(scopes))
        return f"https://oauth.example.test/{provider.lower()}?state={state}"

    def exchange_code(self, provider, code, redirect_uri, requested_scopes):
        self.exchange_calls.append((provider, code, redirect_uri))
        if self.fail_exchange:
            raise OAuthConnectionError("OAUTH_PROVIDER_REJECTED")
        scopes = frozenset(requested_scopes if self.scope_override is None else self.scope_override)
        if provider == "LINKEDIN":
            refresh = LINKEDIN_REFRESH if self.include_linkedin_refresh else None
            return OAuthTokenResponse(
                access_token=LINKEDIN_ACCESS,
                expires_in=self.expiry_seconds,
                scopes=scopes,
                refresh_token=refresh,
                refresh_expires_in=365 * 24 * 60 * 60 if refresh else None,
            )
        return OAuthTokenResponse(
            access_token=INSTAGRAM_ACCESS,
            expires_in=self.expiry_seconds,
            scopes=scopes,
        )

    def identify(self, provider, access_token):
        if provider == "LINKEDIN":
            identity = {
                "provider_subject": MEMBER_URN.rsplit(":", 1)[1],
                "account_id": MEMBER_URN,
                "account_name": "Event Marketing Member",
            }
        else:
            identity = {
                "provider_subject": ACCOUNT_ID,
                "account_id": ACCOUNT_ID,
                "account_name": USERNAME,
                "account_type": "Business",
            }
        return self.account_override or identity

    def refresh(self, provider, payload, scopes):
        with self._lock:
            self.refresh_calls.append((provider, payload["access_token"]))
        if self.refresh_delay:
            time.sleep(self.refresh_delay)
        if self.refresh_fail:
            raise OAuthConnectionError("OAUTH_PROVIDER_REJECTED")
        token = (
            "LINKEDIN_REFRESHED_ACCESS_VALUE"
            if provider == "LINKEDIN"
            else "INSTAGRAM_REFRESHED_ACCESS_VALUE"
        )
        return OAuthTokenResponse(
            access_token=token,
            expires_in=60 * 24 * 60 * 60,
            scopes=frozenset(scopes),
            refresh_token="LINKEDIN_ROTATED_REFRESH_VALUE" if provider == "LINKEDIN" else None,
            refresh_expires_in=300 * 24 * 60 * 60 if provider == "LINKEDIN" else None,
        )

    def revoke(self, provider, payload):
        return self.revoke_result if provider == "LINKEDIN" else None


def _configure(app, settings, provider="LINKEDIN"):
    settings.credential_vault_active_key_id = "test-key"
    if provider == "LINKEDIN":
        settings.linkedin_client_id = "synthetic-linkedin-client"
        settings.linkedin_client_secret = SecretStr("synthetic-linkedin-client-secret")
        settings.linkedin_redirect_uri = "https://event-marketing.test/auth/linkedin/callback"
    else:
        settings.instagram_client_id = "synthetic-instagram-client"
        settings.instagram_client_secret = SecretStr("synthetic-instagram-client-secret")
        settings.instagram_redirect_uri = "https://event-marketing.test/auth/instagram/callback"
        settings.instagram_professional_account_id = ACCOUNT_ID
    gateway = FakeOAuthGateway()
    app.state.oauth_connections.gateway = gateway
    return gateway


def _begin(client, settings, provider_slug):
    response = client.post(
        f"/api/oauth/{provider_slug}/connect", headers=_headers(client, settings)
    )
    assert response.status_code == 200, response.text
    url = urlsplit(response.json()["authorization_url"])
    state = parse_qs(url.query)["state"][0]
    return state


def _callback(client, provider_slug, state, code="synthetic-oauth-code"):
    return client.get(f"/auth/{provider_slug}/callback", params={"state": state, "code": code})


def _rows(app, provider):
    with app.state.database.sessions() as db:
        connection = db.scalar(
            select(ProviderConnection).where(ProviderConnection.provider == provider)
        )
        envelope = (
            db.scalar(
                select(CredentialEnvelope).where(CredentialEnvelope.connection_id == connection.id)
            )
            if connection
            else None
        )
        return connection, envelope


def test_linkedin_oauth_state_and_encrypted_credential_round_trip(app, client, settings, provider):
    _configure(app, settings)
    sign_in(client, provider)
    state = _begin(client, settings, "linkedin")
    response = _callback(client, "linkedin", state)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?connection=linkedin_connected"

    connection, envelope = _rows(app, "LINKEDIN")
    assert connection.status == "CONNECTED"
    assert connection.account_id == MEMBER_URN
    assert set(connection.granted_scopes) == LINKEDIN_SCOPES
    assert envelope is not None
    assert LINKEDIN_ACCESS.encode() not in envelope.ciphertext
    assert LINKEDIN_REFRESH.encode() not in envelope.ciphertext
    assert LINKEDIN_ACCESS not in repr(connection)
    assert "SUPER_SECRET" not in repr(envelope)

    value = app.state.credential_vault.retrieve("LINKEDIN", connection.id, envelope)
    assert value == {"access_token": LINKEDIN_ACCESS, "token_type": "Bearer"}
    grant = app.state.execution_service.linkedin_credentials.access_grant()
    assert grant.access_token == LINKEDIN_ACCESS
    assert grant.member_urn == MEMBER_URN
    assert grant.scopes == LINKEDIN_SCOPES

    api_value = client.get("/api/settings/oauth-connections").json()
    assert api_value["linkedin"]["status"] == "CONNECTED"
    encoded = json.dumps(api_value)
    assert "SUPER_SECRET" not in encoded
    assert "test-key" not in encoded


def test_instagram_oauth_checks_professional_account_and_uses_vault_provider(
    app, client, settings, provider
):
    gateway = _configure(app, settings, "INSTAGRAM")
    sign_in(client, provider)
    state = _begin(client, settings, "instagram")
    response = _callback(client, "instagram", state)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?connection=instagram_connected"
    connection, envelope = _rows(app, "INSTAGRAM")
    assert connection.account_id == ACCOUNT_ID
    assert connection.account_type == "Business"
    assert set(connection.granted_scopes) == INSTAGRAM_SCOPES
    assert INSTAGRAM_ACCESS.encode() not in envelope.ciphertext
    assert (
        app.state.credential_vault.retrieve("INSTAGRAM", connection.id, envelope)["access_token"]
        == INSTAGRAM_ACCESS
    )
    grant = app.state.execution_service.instagram_credentials.access_grant()
    assert grant.access_token == INSTAGRAM_ACCESS
    assert grant.account_id == ACCOUNT_ID
    assert grant.username == USERNAME
    assert gateway.exchange_calls[0][0] == "INSTAGRAM"


def test_state_is_single_use_session_bound_and_callback_never_stores_code(app, settings, provider):
    gateway = _configure(app, settings)
    first = TestClient(app, base_url="http://localhost:5173", follow_redirects=False)
    second = TestClient(app, base_url="http://localhost:5173", follow_redirects=False)
    try:
        sign_in(first, provider)
        sign_in(second, provider)
        state = _begin(first, settings, "linkedin")
        cross_session = _callback(second, "linkedin", state)
        assert cross_session.status_code == 303
        assert cross_session.headers["location"] == "/settings?connection=linkedin_failed"
        assert _rows(app, "LINKEDIN") == (None, None)

        state = _begin(first, settings, "linkedin")
        bad = _callback(first, "linkedin", "A" + state[1:])
        assert bad.status_code == 303
        assert _rows(app, "LINKEDIN") == (None, None)

        state = _begin(first, settings, "linkedin")
        complete = _callback(first, "linkedin", state)
        replay = _callback(first, "linkedin", state)
        assert complete.headers["location"].endswith("linkedin_connected")
        assert replay.headers["location"].endswith("linkedin_failed")
        assert len(gateway.exchange_calls) == 1
        with app.state.database.sessions() as db:
            attempts = list(db.scalars(select(OAuthAuthorizationAttempt)))
            assert all(state not in row.state_digest for row in attempts)
            assert all("synthetic-oauth-code" not in row.state_digest for row in attempts)
            assert all(row.return_destination == "SETTINGS" for row in attempts)
    finally:
        first.close()
        second.close()


def test_expired_state_is_rejected(app, client, settings, provider):
    _configure(app, settings)
    sign_in(client, provider)
    state = _begin(client, settings, "linkedin")
    with app.state.database.sessions.begin() as db:
        row = db.scalar(select(OAuthAuthorizationAttempt))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    response = _callback(client, "linkedin", state)
    assert response.headers["location"].endswith("linkedin_failed")
    assert _rows(app, "LINKEDIN") == (None, None)


def test_failed_reconnect_preserves_old_credential(app, client, settings, provider):
    gateway = _configure(app, settings)
    sign_in(client, provider)
    first_state = _begin(client, settings, "linkedin")
    _callback(client, "linkedin", first_state)
    before_connection, before_envelope = _rows(app, "LINKEDIN")
    before = (before_envelope.key_id, before_envelope.nonce, before_envelope.ciphertext)

    gateway.fail_exchange = True
    reconnect_state = _begin(client, settings, "linkedin")
    response = _callback(client, "linkedin", reconnect_state)
    after_connection, after_envelope = _rows(app, "LINKEDIN")
    assert response.headers["location"].endswith("linkedin_failed")
    assert after_connection.id == before_connection.id
    assert after_connection.status == "CONNECTED"
    assert (after_envelope.key_id, after_envelope.nonce, after_envelope.ciphertext) == before
    assert (
        app.state.credential_vault.retrieve("LINKEDIN", after_connection.id, after_envelope)[
            "access_token"
        ]
        == LINKEDIN_ACCESS
    )


def test_disconnect_deletes_envelope_and_blocks_future_access(app, client, settings, provider):
    _configure(app, settings)
    sign_in(client, provider)
    state = _begin(client, settings, "linkedin")
    _callback(client, "linkedin", state)
    response = client.post("/api/oauth/linkedin/disconnect", headers=_headers(client, settings))
    assert response.status_code == 200
    assert response.json() == {"status": "DISCONNECTED", "remote_revocation": "CONFIRMED"}
    connection, envelope = _rows(app, "LINKEDIN")
    assert connection.status == "DISCONNECTED"
    assert envelope is None
    assert not app.state.oauth_connections.has_active_credentials("LINKEDIN")
    with pytest.raises(LinkedInError):
        app.state.execution_service.linkedin_credentials.access_grant()


def test_key_nonce_aad_and_rotation_invariants(app, client, settings, provider):
    _configure(app, settings)
    old_provider = InMemoryCredentialKeyProvider({"old": KEY_OLD}, "old")
    app.state.credential_vault.key_provider = old_provider
    sign_in(client, provider)
    state = _begin(client, settings, "linkedin")
    _callback(client, "linkedin", state)
    connection, envelope = _rows(app, "LINKEDIN")

    same = {"access_token": LINKEDIN_ACCESS, "token_type": "Bearer"}
    left = app.state.credential_vault._encrypt("LINKEDIN", connection.id, envelope.id, same)
    right = app.state.credential_vault._encrypt("LINKEDIN", connection.id, envelope.id, same)
    assert left[1] != right[1]
    assert left[2] != right[2]

    with pytest.raises(CredentialVaultError):
        app.state.credential_vault.retrieve("INSTAGRAM", connection.id, envelope)
    wrong_key_vault = CredentialVault(InMemoryCredentialKeyProvider({"old": KEY_NEW}, "old"))
    with pytest.raises(CredentialVaultError):
        wrong_key_vault.retrieve("LINKEDIN", connection.id, envelope)

    app.state.credential_vault.key_provider = InMemoryCredentialKeyProvider(
        {"old": KEY_OLD, "new": KEY_NEW}, "new"
    )
    with app.state.database.sessions.begin() as db:
        row = db.scalar(
            select(CredentialEnvelope).where(CredentialEnvelope.id == envelope.id).with_for_update()
        )
        owner = db.get(ProviderConnection, connection.id, with_for_update=True)
        assert app.state.credential_vault.rotate_envelope(db, owner, row)
    _, rotated = _rows(app, "LINKEDIN")
    assert rotated.key_id == "new"
    assert (
        app.state.credential_vault.retrieve("LINKEDIN", connection.id, rotated)["access_token"]
        == LINKEDIN_ACCESS
    )


def test_vault_rejects_tampered_ciphertext_nonce_and_envelope_identity(
    app, client, settings, provider
):
    _configure(app, settings)
    sign_in(client, provider)
    state = _begin(client, settings, "linkedin")
    _callback(client, "linkedin", state)
    connection, envelope = _rows(app, "LINKEDIN")
    original_ciphertext, original_nonce, original_id = (
        envelope.ciphertext,
        envelope.nonce,
        envelope.id,
    )

    envelope.ciphertext = original_ciphertext[:-1] + bytes([original_ciphertext[-1] ^ 1])
    with pytest.raises(CredentialVaultError):
        app.state.credential_vault.retrieve("LINKEDIN", connection.id, envelope)
    envelope.ciphertext = original_ciphertext

    envelope.nonce = bytes([original_nonce[0] ^ 1]) + original_nonce[1:]
    with pytest.raises(CredentialVaultError):
        app.state.credential_vault.retrieve("LINKEDIN", connection.id, envelope)
    envelope.nonce = original_nonce

    envelope.id = uuid.uuid4()
    with pytest.raises(CredentialVaultError):
        app.state.credential_vault.retrieve("LINKEDIN", connection.id, envelope)
    envelope.id = original_id


def test_file_key_provider_rejects_malformed_keyring_and_production_path_in_repo(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"keys": {"k1": base64.b64encode(b"short").decode()}}))
    repo = tmp_path / "repo"
    repo.mkdir()
    provider = FileCredentialKeyProvider(path, "k1", repo)
    with pytest.raises(CredentialVaultError):
        provider.active_key_id()

    duplicate = tmp_path / "duplicate.json"
    encoded = base64.b64encode(KEY_OLD).decode()
    duplicate.write_text(f'{{"keys":{{"k1":"{encoded}"}},"keys":{{"k1":"{encoded}"}}}}')
    provider = FileCredentialKeyProvider(duplicate, "k1", repo)
    with pytest.raises(CredentialVaultError):
        provider.active_key_id()

    inside = repo / "keys.json"
    inside.write_text(json.dumps({"keys": {"k1": base64.b64encode(KEY_OLD).decode()}}))
    provider = FileCredentialKeyProvider(inside, "k1", repo)
    with pytest.raises(CredentialVaultError):
        provider.active_key_id()


def test_linkedin_refresh_is_serialized_and_uses_newest_envelope(app, client, settings, provider):
    gateway = _configure(app, settings)
    gateway.include_linkedin_refresh = True
    gateway.expiry_seconds = 60 * 60
    gateway.refresh_delay = 0.1
    sign_in(client, provider)
    state = _begin(client, settings, "linkedin")
    _callback(client, "linkedin", state)

    credentials = app.state.execution_service.linkedin_credentials
    with ThreadPoolExecutor(max_workers=2) as executor:
        grants = list(executor.map(lambda _index: credentials.access_grant(), range(2)))
    assert len(gateway.refresh_calls) == 1
    assert all(grant.access_token == "LINKEDIN_REFRESHED_ACCESS_VALUE" for grant in grants)
    connection, envelope = _rows(app, "LINKEDIN")
    payload = app.state.credential_vault.retrieve("LINKEDIN", connection.id, envelope)
    assert payload["refresh_token"] == "LINKEDIN_ROTATED_REFRESH_VALUE"
    assert connection.last_refreshed_at is not None


def test_instagram_refresh_only_when_old_long_lived_token_still_valid(
    app, client, settings, provider
):
    gateway = _configure(app, settings, "INSTAGRAM")
    gateway.expiry_seconds = 30 * 24 * 60 * 60
    sign_in(client, provider)
    state = _begin(client, settings, "instagram")
    _callback(client, "instagram", state)
    credentials = app.state.execution_service.instagram_credentials
    assert credentials.access_grant().access_token == INSTAGRAM_ACCESS
    assert gateway.refresh_calls == []

    connection, _envelope = _rows(app, "INSTAGRAM")
    with app.state.database.sessions.begin() as db:
        row = db.get(ProviderConnection, connection.id, with_for_update=True)
        row.connected_at = datetime.now(UTC) - timedelta(days=25)
        row.access_expires_at = datetime.now(UTC) + timedelta(days=5)
    grant = credentials.access_grant()
    assert grant.access_token == "INSTAGRAM_REFRESHED_ACCESS_VALUE"
    assert gateway.refresh_calls[0][0] == "INSTAGRAM"


def test_anonymous_oauth_and_account_mismatch_do_not_store_credentials(
    app, client, settings, provider
):
    gateway = _configure(app, settings, "INSTAGRAM")
    assert client.post("/api/oauth/instagram/connect").status_code == 401
    sign_in(client, provider)
    state = _begin(client, settings, "instagram")
    gateway.account_override = {
        "provider_subject": "17841400000000999",
        "account_id": "17841400000000999",
        "account_name": "other_account",
        "account_type": "Business",
    }
    response = _callback(client, "instagram", state)
    assert response.headers["location"].endswith("instagram_failed")
    assert _rows(app, "INSTAGRAM") == (None, None)


def test_provider_denial_and_missing_scope_do_not_replace_connection(
    app, client, settings, provider
):
    gateway = _configure(app, settings)
    sign_in(client, provider)
    state = _begin(client, settings, "linkedin")
    denied = client.get(
        "/auth/linkedin/callback", params={"state": state, "error": "access_denied"}
    )
    assert denied.headers["location"].endswith("linkedin_denied")
    assert gateway.exchange_calls == []
    assert _rows(app, "LINKEDIN") == (None, None)

    gateway.scope_override = frozenset()
    state = _begin(client, settings, "linkedin")
    rejected = _callback(client, "linkedin", state)
    assert rejected.headers["location"].endswith("linkedin_failed")
    assert _rows(app, "LINKEDIN") == (None, None)


@pytest.mark.parametrize("provider_slug", ["linkedin", "instagram"])
def test_provider_401_marks_saved_connection_for_reauthorization(
    app, client, settings, provider, provider_slug
):
    provider_key = provider_slug.upper()
    _configure(app, settings, provider_key)
    sign_in(client, provider)
    state = _begin(client, settings, provider_slug)
    _callback(client, provider_slug, state)
    credentials = getattr(
        app.state.execution_service,
        "linkedin_credentials" if provider_slug == "linkedin" else "instagram_credentials",
    )
    response_body = (
        {"error": {"code": 190, "message": "synthetic rejection"}}
        if provider_slug == "instagram"
        else {"message": "synthetic rejection"}
    )
    http_client = httpx2.Client(
        transport=httpx2.MockTransport(lambda _request: httpx2.Response(401, json=response_body))
    )
    try:
        if provider_slug == "linkedin":
            api_client = LinkedInRestClient(
                settings,
                http_client=http_client,
                on_authentication_failure=credentials.mark_reauthorization_required,
            )
            with pytest.raises(LinkedInError) as error:
                api_client.request("GET", "/test", LINKEDIN_ACCESS)
        else:
            api_client = InstagramAPIClient(
                settings,
                http_client=http_client,
                sleeper=lambda _seconds: None,
                on_authentication_failure=credentials.mark_reauthorization_required,
            )
            with pytest.raises(InstagramError) as error:
                api_client.request("GET", "me", INSTAGRAM_ACCESS)
        assert error.value.category == "AUTHENTICATION"
        connection, envelope = _rows(app, provider_key)
        assert connection.status == "REAUTH_REQUIRED"
        assert connection.reauthorization_required_at is not None
        assert envelope is not None
        assert (
            app.state.oauth_connections.metadata(provider_slug, connection.owner_user_id)["status"]
            == "REAUTH_REQUIRED"
        )
    finally:
        http_client.close()
