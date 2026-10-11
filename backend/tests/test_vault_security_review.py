"""Regression tests added by the WP1M security review (attack scenarios not previously covered)."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app import auth
from app.config import Settings
from app.credential_vault import (
    CredentialVault,
    CredentialVaultError,
    InMemoryCredentialKeyProvider,
)
from app.instagram import InstagramError
from app.linkedin import LinkedInError
from app.main import create_app
from app.models import CredentialEnvelope, ProviderConnection, ServerSession
from app.oauth_connections import OAuthConnectionError
from scripts import credential_vault as vault_cli
from tests.conftest import sign_in
from tests.test_oauth_vault import KEY_NEW, KEY_OLD, _begin, _callback, _configure, _rows


def _current(app) -> auth.CurrentSession:
    with app.state.database.sessions() as db:
        session = db.scalar(select(ServerSession).where(ServerSession.kind == "APP_SESSION"))
        return auth.CurrentSession(session.id, session.user_id, "Test Member", "csrf")


def test_concurrent_callbacks_with_one_state_consume_it_exactly_once(
    app, client, settings, provider
):
    _configure(app, settings)
    sign_in(client, provider)
    state = _begin(client, settings, "linkedin")
    current = _current(app)
    service = app.state.oauth_connections
    barrier = threading.Barrier(8)

    def attempt(_index):
        barrier.wait()
        try:
            return service.consume("linkedin", state, current)
        except OAuthConnectionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))
    assert sum(result is not None for result in results) == 1


def test_ciphertext_cannot_be_transplanted_between_providers_or_connections(
    app, client, settings, provider
):
    _configure(app, settings)
    sign_in(client, provider)
    _callback(client, "linkedin", _begin(client, settings, "linkedin"))
    _configure(app, settings, "INSTAGRAM")
    _callback(client, "instagram", _begin(client, settings, "instagram"))
    linkedin, linkedin_envelope = _rows(app, "LINKEDIN")
    instagram, instagram_envelope = _rows(app, "INSTAGRAM")
    vault = app.state.credential_vault

    # Copy Instagram's nonce+ciphertext into LinkedIn's envelope row (same key ID).
    with app.state.database.sessions.begin() as db:
        db.execute(
            text(
                "UPDATE credential_envelopes SET nonce = :n, ciphertext = :c, key_id = :k "
                "WHERE id = :id"
            ),
            {
                "n": instagram_envelope.nonce,
                "c": instagram_envelope.ciphertext,
                "k": instagram_envelope.key_id,
                "id": linkedin_envelope.id,
            },
        )
    _, swapped = _rows(app, "LINKEDIN")
    with pytest.raises(CredentialVaultError):
        vault.retrieve("LINKEDIN", linkedin.id, swapped)
    with pytest.raises(LinkedInError):
        app.state.execution_service.linkedin_credentials.access_grant()

    # Move the whole Instagram envelope row (keeping its ID) under the LinkedIn connection.
    with app.state.database.sessions.begin() as db:
        db.execute(text("DELETE FROM credential_envelopes WHERE id = :id"), {"id": swapped.id})
        db.execute(
            text("UPDATE credential_envelopes SET connection_id = :c WHERE id = :id"),
            {"c": linkedin.id, "id": instagram_envelope.id},
        )
    _, moved = _rows(app, "LINKEDIN")
    assert moved.id == instagram_envelope.id
    with pytest.raises(CredentialVaultError):
        vault.retrieve("LINKEDIN", linkedin.id, moved)
    with pytest.raises(LinkedInError):
        app.state.execution_service.linkedin_credentials.access_grant()
    # Instagram now has no envelope and is unavailable rather than misusing anything.
    with pytest.raises(InstagramError):
        app.state.execution_service.instagram_credentials.access_grant()


def test_rotation_uses_connection_then_envelope_lock_order(app, client, settings, provider):
    _configure(app, settings)
    app.state.credential_vault.key_provider = InMemoryCredentialKeyProvider({"old": KEY_OLD}, "old")
    sign_in(client, provider)
    _callback(client, "linkedin", _begin(client, settings, "linkedin"))
    connection, envelope = _rows(app, "LINKEDIN")
    rotating_vault = CredentialVault(
        InMemoryCredentialKeyProvider({"old": KEY_OLD, "new": KEY_NEW}, "new")
    )
    database = app.state.database
    result: dict = {}

    holder = database.sessions()
    holder.begin()
    # Simulate live credential use holding the connection lock (as access_grant does).
    holder.execute(
        select(ProviderConnection).where(ProviderConnection.id == connection.id).with_for_update()
    )
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "counts", vault_cli._rotate(settings, database=database, vault=rotating_vault)
        )
    )
    worker.start()
    worker.join(timeout=1.0)
    assert worker.is_alive()  # rotation waits on the connection lock...
    # ...and does not hold the envelope lock meanwhile, so no lock-order deadlock exists.
    holder.execute(
        select(CredentialEnvelope)
        .where(CredentialEnvelope.id == envelope.id)
        .with_for_update(nowait=True)
    )
    holder.commit()
    holder.close()
    worker.join(timeout=10)
    assert result["counts"] == (1, 0)
    _, rotated = _rows(app, "LINKEDIN")
    assert rotated.key_id == "new"
    assert rotating_vault.retrieve("LINKEDIN", connection.id, rotated)["token_type"] == "Bearer"


def test_rotation_failure_reports_partial_progress_without_secrets(
    app, client, settings, provider, capsys, monkeypatch
):
    _configure(app, settings)
    app.state.credential_vault.key_provider = InMemoryCredentialKeyProvider({"old": KEY_OLD}, "old")
    sign_in(client, provider)
    _callback(client, "linkedin", _begin(client, settings, "linkedin"))
    # The active key cannot decrypt the stored "old" envelope: rotation must stop honestly.
    broken = CredentialVault(InMemoryCredentialKeyProvider({"new": KEY_NEW}, "new"))
    with pytest.raises(vault_cli.RotationIncomplete) as raised:
        vault_cli._rotate(settings, database=app.state.database, vault=broken)
    assert (raised.value.rotated, raised.value.unchanged) == (0, 0)
    _, envelope = _rows(app, "LINKEDIN")
    assert envelope.key_id == "old"  # untouched; nothing destroyed


def test_test_mode_is_loopback_only_and_has_no_shared_vault_key(settings):
    values = settings.model_dump()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **(values | {"app_origin": "https://business.example.test"}))
    first = create_app(Settings(_env_file=None, **values))
    second = create_app(Settings(_env_file=None, **values))
    first_key = first.state.credential_vault.key_provider.key_for("test-key")
    second_key = second.state.credential_vault.key_provider.key_for("test-key")
    assert first_key != second_key and len(first_key) == 32


def test_non_test_environment_mounts_no_test_routes(settings):
    development = Settings(
        _env_file=None, **(settings.model_dump() | {"environment": "development"})
    )
    app = create_app(development)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(path.startswith("/api/test") for path in paths)
    assert app.state.group_ranker_scenarios is None
    assert app.state.execution_service.scenarios is None
    # Without an external keyring the vault is simply unavailable (fail closed, no key created).
    assert app.state.credential_vault.is_configured() is False
