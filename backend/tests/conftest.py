from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import create_engine, text

from app.config import Settings
from app.main import create_app
from app.oidc import ISSUER, GoogleOIDC
from tests.database import disposable_database, drop_database, migrate


@pytest.fixture(scope="session")
def database_url():
    url, admin, name = disposable_database()
    migrate(url)
    yield url
    drop_database(admin, name)


@pytest.fixture
def settings(database_url, tmp_path):
    return Settings(
        _env_file=None,
        environment="test",
        image_generation_mode="fake",
        app_origin="http://localhost:5173",
        database_url=database_url,
        google_client_id="test-client",
        google_client_secret="test-only",
        allowed_emails=["member@example.test"],
        private_storage_dir=tmp_path / "private",
    )


@pytest.fixture
def provider(settings):
    class Provider:
        def __init__(self):
            self.key = RSAKey.generate_key(2048, {"kid": "fixture"})
            self.changes = {}
            self.nonce = None
            self.requests = []
            self.corrupt_signature = False
            self.failure = False

        def handle(self, request):
            import time

            self.requests.append(request)
            if request.url.path.endswith("/certs"):
                return httpx2.Response(200, json={"keys": [self.key.as_dict(private=False)]})
            if self.failure:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            claims = {
                "iss": ISSUER,
                "aud": settings.google_client_id,
                "sub": "fixture-subject",
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
                "nonce": self.nonce,
                "email": "member@example.test",
                "email_verified": True,
                "name": "Test Member",
            }
            claims.update(self.changes)
            key = (
                RSAKey.generate_key(2048, {"kid": "fixture"})
                if self.corrupt_signature
                else self.key
            )
            encoded = jwt.encode({"alg": "RS256", "kid": "fixture"}, claims, key)
            return httpx2.Response(
                200,
                json={
                    "id_token": encoded,
                    "access_token": "fixture-provider-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )

    fixture = Provider()
    fixture.oidc = GoogleOIDC(settings, httpx2.MockTransport(fixture.handle))
    return fixture


@pytest.fixture
def app(settings, provider):
    engine = create_engine(settings.database_url.get_secret_value())
    with engine.begin() as db:
        db.execute(text("TRUNCATE campaign_versions, campaigns CASCADE"))
        db.execute(text("DELETE FROM credential_envelopes"))
        db.execute(text("DELETE FROM oauth_authorization_attempts"))
        db.execute(text("DELETE FROM provider_connections"))
        db.execute(text("DELETE FROM audit_events"))
        db.execute(text("TRUNCATE facebook_group_records CASCADE"))
        db.execute(text("DELETE FROM source_materials"))
        db.execute(text("DELETE FROM events"))
        db.execute(text("DELETE FROM sessions"))
        db.execute(text("DELETE FROM app_users"))
    engine.dispose()
    return create_app(settings, provider.oidc)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://localhost:5173", follow_redirects=False) as client:
        yield client


def begin_login(client, provider):
    response = client.get("/auth/login")
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    provider.nonce = query["nonce"][0]
    return query


def sign_in(client, provider):
    query = begin_login(client, provider)
    return client.get("/auth/callback", params={"state": query["state"][0], "code": "fixture-code"})
