import secrets
import time
from datetime import timedelta
from urllib.parse import parse_qs

import pytest
from alembic import command
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect, select

from app.auth import digest, now
from app.config import Settings
from app.main import create_app
from app.models import AppUser, ServerSession
from tests.conftest import begin_login, sign_in
from tests.database import disposable_database, drop_database, migrate


def test_migration_from_empty_repeat_and_downgrade():
    url, admin, name = disposable_database()
    from sqlalchemy import create_engine

    engine = create_engine(url)
    try:
        assert inspect(engine).get_table_names() == []
        cfg = migrate(url)
        command.upgrade(cfg, "head")
        assert set(inspect(engine).get_table_names()) == {
            "app_users",
            "sessions",
            "events",
            "source_materials",
            "campaigns",
            "campaign_versions",
            "approvals",
            "destination_executions",
            "execution_attempts",
            "audit_events",
            "assets",
            "campaign_version_assets",
            "facebook_group_records",
            "group_recommendations",
            "group_decisions",
            "destination_resource_bindings",
            "provider_connections",
            "credential_envelopes",
            "oauth_authorization_attempts",
            "alembic_version",
        }
        command.check(cfg)
        command.downgrade(cfg, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        command.upgrade(cfg, "head")
        assert "sessions" in inspect(engine).get_table_names()
        assert "campaign_versions" in inspect(engine).get_table_names()
        assert "approvals" in inspect(engine).get_table_names()
        assert "destination_executions" in inspect(engine).get_table_names()
        assert "assets" in inspect(engine).get_table_names()
        assert "campaign_version_assets" in inspect(engine).get_table_names()
        assert "group_decisions" in inspect(engine).get_table_names()
        assert "provider_connections" in inspect(engine).get_table_names()
        assert "credential_envelopes" in inspect(engine).get_table_names()
        assert "oauth_authorization_attempts" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        drop_database(admin, name)


def test_public_health_and_private_authority(client):
    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/health/ready").json() == {"status": "ready"}
    assert client.get("/api/me").status_code == 401
    client.cookies.set("isLoggedIn", "true")
    assert client.get("/api/me", headers={"Authorization": "Bearer invented"}).status_code == 401
    assert client.get("/docs").status_code == 404
    assert client.get("/api/me").headers["cache-control"] == "no-store"


def test_login_and_opaque_session(client, provider, app, settings):
    response = sign_in(client, provider)
    assert response.headers["location"] == "/"
    token = client.cookies.get(settings.session_cookie)
    assert len(token) == 43 and token.count(".") == 0
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Domain=" not in cookie
    assert "Secure" not in cookie  # Explicit loopback HTTP only.
    result = client.get("/api/me").json()
    assert set(result) == {"display_name", "csrf_token"}
    assert result["display_name"] == "Test Member"
    assert token not in str(result) and "fixture-provider-token" not in str(result)
    with app.state.database.sessions() as db:
        session = db.scalar(select(ServerSession).where(ServerSession.kind == "APP_SESSION"))
        assert session.token_digest == digest(token)
        assert token not in str(session.private_data)
        assert "fixture-provider-token" not in str(session.private_data)
        user = db.get(AppUser, session.user_id)
        assert user.issuer == "https://accounts.google.com" and user.subject == "fixture-subject"
    body = parse_qs(provider.requests[0].content.decode())
    assert "code_verifier" in body and body["code"] == ["fixture-code"]


def test_no_public_signup(client, provider, app):
    provider.changes["email"] = "outsider@example.test"
    response = sign_in(client, provider)
    assert response.headers["location"].startswith("/login?error=")
    assert client.get("/api/me").status_code == 401
    with app.state.database.sessions() as db:
        assert db.scalar(select(AppUser)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.test"},
        {"aud": "another-client"},
        {"exp": 1},
        {"nonce": "wrong"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"iat": int(time.time()) + 3600},
        {"sub": ""},
        {"aud": ["test-client", "another-client"]},
        {"azp": "another-client"},
        {"at_hash": "incorrect"},
    ],
)
def test_invalid_oidc_claims_cannot_authenticate(client, provider, changes):
    provider.changes = changes
    assert sign_in(client, provider).headers["location"].startswith("/login?error=")
    assert client.get("/api/me").status_code == 401


def test_bad_signature_and_provider_response(client, provider):
    provider.corrupt_signature = True
    assert sign_in(client, provider).headers["location"].startswith("/login?error=")
    provider.corrupt_signature = False
    provider.failure = True
    assert sign_in(client, provider).headers["location"].startswith("/login?error=")
    assert client.get("/api/me").status_code == 401


def test_state_browser_binding_and_replay(client, provider, app):
    query = begin_login(client, provider)
    assert query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    with TestClient(app, base_url="http://localhost:5173", follow_redirects=False) as other:
        assert (
            other.get("/auth/callback", params={"state": query["state"][0], "code": "x"})
            .headers["location"]
            .startswith("/login?error=")
        )
    response = client.get(
        "/auth/callback", params={"state": secrets.token_urlsafe(32), "code": "x"}
    )
    assert response.headers["location"].startswith("/login?error=")
    assert not provider.requests
    assert (
        client.get("/auth/callback", params={"state": query["state"][0], "code": "x"})
        .headers["location"]
        .startswith("/login?error=")
    )
    assert client.get("/api/me").status_code == 401


def test_successful_callback_cannot_replay(client, provider, settings):
    query = begin_login(client, provider)
    flow_cookie = client.cookies.get(settings.flow_cookie)
    response = client.get("/auth/callback", params={"state": query["state"][0], "code": "x"})
    assert response.headers["location"] == "/"
    token = client.cookies.get(settings.session_cookie)
    client.cookies.set(settings.flow_cookie, flow_cookie)
    assert (
        client.get("/auth/callback", params={"state": query["state"][0], "code": "x"})
        .headers["location"]
        .startswith("/login?error=")
    )
    assert client.cookies.get(settings.session_cookie) == token


@pytest.mark.parametrize("change", ["expiry", "idle", "revoked", "disabled", "removed_allowlist"])
def test_expired_revoked_disabled_or_deauthorized_session(client, provider, app, settings, change):
    sign_in(client, provider)
    with app.state.database.sessions.begin() as db:
        row = db.scalar(select(ServerSession).where(ServerSession.kind == "APP_SESSION"))
        if change == "expiry":
            row.expires_at = now() - timedelta(seconds=1)
        if change == "idle":
            row.last_seen_at = now() - timedelta(hours=2)
        if change == "revoked":
            row.revoked_at = now()
        if change == "disabled":
            db.get(AppUser, row.user_id).enabled = False
        if change == "removed_allowlist":
            settings.allowed_emails = []
    assert client.get("/api/me").status_code == 401
    assert not client.cookies.get(settings.session_cookie)


def test_stable_subject_not_mutable_email(client, provider, app):
    sign_in(client, provider)
    provider.changes["email"] = "renamed@example.test"
    assert sign_in(client, provider).headers["location"] == "/"
    with app.state.database.sessions() as db:
        assert len(db.scalars(select(AppUser)).all()) == 1
    provider.changes = {"sub": "recycled-address-owner"}
    assert sign_in(client, provider).headers["location"].startswith("/login?error=")


def test_logout_csrf_origin_and_revocation(client, provider, app, settings):
    sign_in(client, provider)
    old = client.cookies.get(settings.session_cookie)
    csrf = client.get("/api/me").json()["csrf_token"]
    assert client.post("/api/logout").status_code == 403
    assert (
        client.post(
            "/api/logout", headers={"origin": "https://evil.test", "x-csrf-token": csrf}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/logout", headers={"origin": settings.app_origin, "x-csrf-token": "wrong"}
        ).status_code
        == 403
    )
    response = client.post(
        "/api/logout", headers={"origin": settings.app_origin, "x-csrf-token": csrf}
    )
    assert response.status_code == 204
    with app.state.database.sessions() as db:
        assert db.scalar(
            select(ServerSession).where(ServerSession.token_digest == digest(old))
        ).revoked_at
    client.cookies.set(settings.session_cookie, old)
    assert client.get("/api/me").status_code == 401


def test_rotation_revokes_previous_session(client, provider, settings):
    sign_in(client, provider)
    first = client.cookies.get(settings.session_cookie)
    sign_in(client, provider)
    second = client.cookies.get(settings.session_cookie)
    assert first != second
    client.cookies.set(settings.session_cookie, first)
    assert client.get("/api/me").status_code == 401


def test_readiness_fails_closed_without_migrations(settings):
    url, admin, name = disposable_database()
    unready = settings.model_copy(update={"database_url": type(settings.database_url)(url)})
    try:
        with TestClient(create_app(unready), base_url=settings.app_origin) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 503
            assert set(client.get("/health/ready").json()) == {"error"}
    finally:
        drop_database(admin, name)


def production_settings(settings, **changes):
    values = dict(
        environment="production",
        app_origin="https://app.example.test",
        database_url="postgresql+psycopg://operator:test-fixture-only@localhost/emp_prod",
        extraction_mode="openai",
        campaign_generation_mode="openai",
        image_generation_mode="openai",
        group_recommendation_mode="openai",
        openai_campaign_model="gpt-6-luna-test",
        openai_api_key="test-provider-key",
    )
    values.update(changes)
    return Settings(_env_file=None, **(settings.model_dump() | values))


@pytest.mark.parametrize(
    "changes",
    [
        {"app_origin": "http://app.example.test"},
        {"google_client_id": "", "google_client_secret": ""},
        {"allowed_emails": []},
        {"dev_auth_enabled": True},
        {"database_url": "sqlite://"},
        {"app_origin": "https://app.example.test/path"},
        {"database_url": "postgresql+psycopg://emp_local:local-only-password@localhost/emp_local"},
    ],
)
def test_unsafe_production_config_rejected(settings, changes):
    with pytest.raises(ValidationError):
        production_settings(settings, **changes)


def test_secure_production_cookies_and_no_provider_substitution(settings, provider):
    production = production_settings(settings)
    with pytest.raises(RuntimeError):
        create_app(production, provider.oidc)
    from starlette.responses import Response

    from app.auth import set_cookie

    response = Response()
    set_cookie(response, production, production.session_cookie, "opaque", 100)
    assert production.session_cookie.startswith("__Host-")
    assert all(
        value in response.headers["set-cookie"]
        for value in ("Secure", "HttpOnly", "SameSite=lax", "Path=/")
    )
    assert "Domain=" not in response.headers["set-cookie"]


def test_unconfigured_development_has_no_bypass(settings):
    settings.google_client_id = ""
    settings.google_client_secret = type(settings.google_client_secret)("")
    with TestClient(create_app(settings), base_url=settings.app_origin) as client:
        assert client.get("/auth/status").json() == {"login_available": False}
        assert client.get("/auth/login").status_code == 503
        assert client.get("/api/me").status_code == 401


def test_logs_do_not_contain_codes_tokens_or_secrets(client, provider, settings, caplog):
    caplog.set_level("INFO")
    sign_in(client, provider)
    assert "fixture-code" not in caplog.text
    assert "fixture-provider-token" not in caplog.text
    assert settings.google_client_secret.get_secret_value() not in caplog.text
    assert client.cookies.get(settings.session_cookie) not in caplog.text
