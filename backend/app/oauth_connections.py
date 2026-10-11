"""State-protected OAuth connections and server-side provider credentials."""

import hashlib
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import unquote, urlencode

import httpx2
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app import auth
from app.credential_vault import CredentialVault, CredentialVaultError
from app.instagram import REQUIRED_SCOPES as INSTAGRAM_SCOPES
from app.linkedin import REQUIRED_SCOPES as LINKEDIN_SCOPES
from app.models import (
    AuditEvent,
    CredentialEnvelope,
    OAuthAuthorizationAttempt,
    ProviderConnection,
    ServerSession,
)

LOGGER = logging.getLogger("event_marketing.oauth")
PROVIDERS = {"linkedin": "LINKEDIN", "instagram": "INSTAGRAM"}
REQUIRED_SCOPES = {"LINKEDIN": LINKEDIN_SCOPES, "INSTAGRAM": INSTAGRAM_SCOPES}
ATTEMPT_TTL = timedelta(minutes=10)
LINKEDIN_REFRESH_WINDOW = timedelta(days=7)
INSTAGRAM_REFRESH_WINDOW = timedelta(days=14)
EXPIRY_SKEW = timedelta(minutes=5)
ACCOUNT_ID = re.compile(r"^[A-Za-z0-9:_-]{1,240}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class OAuthConnectionError(Exception):
    """Safe provider/connection error with a stable internal category."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OAuthTokenResponse:
    access_token: str = field(repr=False)
    expires_in: int
    scopes: frozenset[str]
    refresh_token: str | None = field(default=None, repr=False)
    refresh_expires_in: int | None = None
    token_type: str = "Bearer"

    def secret_payload(self) -> dict:
        result = {"access_token": self.access_token, "token_type": self.token_type}
        if self.refresh_token:
            result["refresh_token"] = self.refresh_token
        return result


class LiveOAuthGateway:
    """Provider-specific OAuth HTTP details; no request or response body is logged."""

    LINKEDIN_AUTHORIZE = "https://www.linkedin.com/oauth/v2/authorization"
    LINKEDIN_TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
    LINKEDIN_REVOKE = "https://www.linkedin.com/oauth/v2/revoke"
    LINKEDIN_USERINFO = "https://api.linkedin.com/v2/userinfo"
    INSTAGRAM_AUTHORIZE = "https://www.instagram.com/oauth/authorize"
    INSTAGRAM_TOKEN = "https://api.instagram.com/oauth/access_token"
    INSTAGRAM_GRAPH = "https://graph.instagram.com"

    def __init__(self, settings, http_client=None):
        self.settings = settings
        self._owns_http = http_client is None
        self.http = http_client or httpx2.Client(
            timeout=15, follow_redirects=False, trust_env=False
        )

    def close(self):
        if self._owns_http:
            self.http.close()

    def authorization_url(self, provider: str, state: str, redirect_uri: str, scopes) -> str:
        if provider == "LINKEDIN":
            if (
                not self.settings.linkedin_client_id
                or not self.settings.linkedin_client_secret.get_secret_value()
            ):
                raise OAuthConnectionError("OAUTH_NOT_CONFIGURED")
            endpoint = self.LINKEDIN_AUTHORIZE
            values = {
                "response_type": "code",
                "client_id": self.settings.linkedin_client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": " ".join(sorted(scopes)),
            }
        else:
            if (
                not self.settings.instagram_client_id
                or not self.settings.instagram_client_secret.get_secret_value()
            ):
                raise OAuthConnectionError("OAUTH_NOT_CONFIGURED")
            endpoint = self.INSTAGRAM_AUTHORIZE
            values = {
                "client_id": self.settings.instagram_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": ",".join(sorted(scopes)),
                "state": state,
            }
        return f"{endpoint}?{urlencode(values)}"

    @staticmethod
    def _json(response, *, accepted=(200,)) -> dict:
        if response.status_code not in accepted:
            if response.status_code == 429:
                raise OAuthConnectionError("OAUTH_RATE_LIMITED")
            if response.status_code in {401, 403}:
                raise OAuthConnectionError("OAUTH_PROVIDER_REJECTED")
            raise OAuthConnectionError("OAUTH_PROVIDER_UNAVAILABLE")
        try:
            body = response.json()
        except Exception:
            raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE") from None
        if not isinstance(body, dict) or isinstance(body.get("error"), (dict, str)):
            raise OAuthConnectionError("OAUTH_PROVIDER_REJECTED")
        return body

    @staticmethod
    def _token(body: dict, requested_scopes) -> OAuthTokenResponse:
        access = body.get("access_token")
        expires = body.get("expires_in")
        scopes = body.get("scope")
        if not isinstance(access, str) or not 1 <= len(access) <= 4096:
            raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE")
        if not isinstance(expires, (int, str)) or isinstance(expires, bool):
            raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE")
        try:
            expires = int(expires)
        except (TypeError, ValueError):
            raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE") from None
        if not 60 <= expires <= 366 * 24 * 60 * 60:
            raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE")
        if scopes is None:
            parsed_scopes = frozenset(requested_scopes)
        elif isinstance(scopes, str):
            parsed_scopes = frozenset(
                part for part in re.split(r"[\s,]+", unquote(scopes).strip()) if part
            )
        elif isinstance(scopes, list) and all(isinstance(item, str) for item in scopes):
            parsed_scopes = frozenset(scopes)
        else:
            raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE")
        refresh = body.get("refresh_token")
        if refresh is not None and (not isinstance(refresh, str) or not 1 <= len(refresh) <= 4096):
            raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE")
        refresh_expires = body.get("refresh_token_expires_in")
        if refresh_expires is not None:
            try:
                refresh_expires = int(refresh_expires)
            except (TypeError, ValueError):
                raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE") from None
            if not 60 <= refresh_expires <= 5 * 366 * 24 * 60 * 60:
                raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE")
        token_type = body.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise OAuthConnectionError("OAUTH_PROVIDER_INVALID_RESPONSE")
        return OAuthTokenResponse(
            access_token=access,
            expires_in=expires,
            scopes=parsed_scopes,
            refresh_token=refresh,
            refresh_expires_in=refresh_expires,
            token_type="Bearer",
        )

    def exchange_code(self, provider: str, code: str, redirect_uri: str, requested_scopes):
        try:
            if provider == "LINKEDIN":
                response = self.http.post(
                    self.LINKEDIN_TOKEN,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self.settings.linkedin_client_id,
                        "client_secret": self.settings.linkedin_client_secret.get_secret_value(),
                    },
                    headers={"Accept": "application/json"},
                )
                return self._token(self._json(response), requested_scopes)

            response = self.http.post(
                self.INSTAGRAM_TOKEN,
                data={
                    "client_id": self.settings.instagram_client_id,
                    "client_secret": self.settings.instagram_client_secret.get_secret_value(),
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            short_token = self._token(self._json(response), requested_scopes)
            long_response = self.http.get(
                f"{self.INSTAGRAM_GRAPH}/access_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": self.settings.instagram_client_secret.get_secret_value(),
                    "access_token": short_token.access_token,
                },
            )
            long_body = self._json(long_response)
            result = self._token(
                {**long_body, "scope": long_body.get("scope", list(short_token.scopes))},
                requested_scopes,
            )
            return result
        except OAuthConnectionError:
            raise
        except Exception:
            raise OAuthConnectionError("OAUTH_PROVIDER_UNAVAILABLE") from None

    def identify(self, provider: str, access_token: str) -> dict:
        try:
            if provider == "LINKEDIN":
                response = self.http.get(
                    self.LINKEDIN_USERINFO,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
                body = self._json(response)
                subject = body.get("sub")
                name = body.get("name")
                if not isinstance(subject, str) or not ACCOUNT_ID.fullmatch(subject):
                    raise OAuthConnectionError("OAUTH_IDENTITY_INVALID")
                return {
                    "provider_subject": subject,
                    "account_id": f"urn:li:person:{subject}",
                    "account_name": name[:240] if isinstance(name, str) and name.strip() else None,
                }

            version = self.settings.instagram_graph_api_version
            response = self.http.get(
                f"{self.INSTAGRAM_GRAPH}/{version}/me",
                params={"fields": "user_id,username,account_type"},
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            body = self._json(response)
            account_id = body.get("user_id") or body.get("id")
            username = body.get("username")
            account_type = body.get("account_type")
            if (
                not isinstance(account_id, (str, int))
                or not re.fullmatch(r"[0-9]{1,32}", str(account_id))
                or account_type not in {"Business", "Media_Creator"}
                or not isinstance(username, str)
                or not re.fullmatch(r"[A-Za-z0-9._]{1,64}", username)
            ):
                raise OAuthConnectionError("OAUTH_IDENTITY_INVALID")
            return {
                "provider_subject": str(account_id),
                "account_id": str(account_id),
                "account_name": username,
                "account_type": account_type,
            }
        except OAuthConnectionError:
            raise
        except Exception:
            raise OAuthConnectionError("OAUTH_PROVIDER_UNAVAILABLE") from None

    def refresh(self, provider: str, payload: dict, scopes) -> OAuthTokenResponse:
        try:
            if provider == "LINKEDIN":
                refresh_token = payload.get("refresh_token")
                if not refresh_token:
                    raise OAuthConnectionError("OAUTH_REAUTH_REQUIRED")
                response = self.http.post(
                    self.LINKEDIN_TOKEN,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self.settings.linkedin_client_id,
                        "client_secret": self.settings.linkedin_client_secret.get_secret_value(),
                    },
                    headers={"Accept": "application/json"},
                )
                return self._token(self._json(response), scopes)
            response = self.http.get(
                f"{self.INSTAGRAM_GRAPH}/refresh_access_token",
                params={
                    "grant_type": "ig_refresh_token",
                    "access_token": payload["access_token"],
                },
            )
            body = self._json(response)
            return self._token({**body, "scope": body.get("scope", list(scopes))}, scopes)
        except OAuthConnectionError:
            raise
        except Exception:
            raise OAuthConnectionError("OAUTH_PROVIDER_UNAVAILABLE") from None

    def revoke(self, provider: str, payload: dict) -> bool | None:
        if provider != "LINKEDIN":
            return None
        try:
            response = self.http.post(
                self.LINKEDIN_REVOKE,
                data={
                    "token": payload["access_token"],
                    "client_id": self.settings.linkedin_client_id,
                    "client_secret": self.settings.linkedin_client_secret.get_secret_value(),
                },
                headers={"Accept": "application/json"},
            )
            return response.is_success
        except Exception:
            return False


class OAuthConnections:
    def __init__(self, database, vault: CredentialVault, settings, gateway):
        self.database, self.vault, self.settings, self.gateway = database, vault, settings, gateway

    def _provider(self, value: str) -> str:
        provider = PROVIDERS.get(value)
        if not provider:
            raise HTTPException(404, "NOT_FOUND")
        return provider

    def _redirect_uri(self, provider: str) -> str:
        return (
            self.settings.linkedin_redirect_uri
            if provider == "LINKEDIN"
            else self.settings.instagram_redirect_uri
        )

    def start(self, provider_slug: str, current: auth.CurrentSession) -> str:
        provider = self._provider(provider_slug)
        if not self.vault.is_configured():
            raise HTTPException(503, "CREDENTIAL_VAULT_UNAVAILABLE")
        redirect_uri = self._redirect_uri(provider)
        if not redirect_uri:
            raise HTTPException(503, "OAUTH_NOT_CONFIGURED")
        state = secrets.token_urlsafe(32)
        scopes = REQUIRED_SCOPES[provider]
        try:
            url = self.gateway.authorization_url(provider, state, redirect_uri, scopes)
        except OAuthConnectionError as error:
            raise HTTPException(503, error.code) from None
        at = utcnow()
        with self.database.sessions.begin() as db:
            session_row = db.scalar(
                select(ServerSession).where(
                    ServerSession.id == current.session_id,
                    ServerSession.user_id == current.user_id,
                    ServerSession.kind == "APP_SESSION",
                    ServerSession.revoked_at.is_(None),
                    ServerSession.expires_at > at,
                )
            )
            if session_row is None:
                raise HTTPException(401, "AUTH_REQUIRED")
            db.add(
                OAuthAuthorizationAttempt(
                    id=uuid.uuid4(),
                    provider=provider,
                    user_id=current.user_id,
                    session_id=current.session_id,
                    state_digest=state_digest(state),
                    return_destination="SETTINGS",
                    created_at=at,
                    expires_at=at + ATTEMPT_TTL,
                )
            )
        return url

    def consume(self, provider_slug: str, state: str, current: auth.CurrentSession) -> str:
        provider = self._provider(provider_slug)
        if not isinstance(state, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state):
            raise OAuthConnectionError("OAUTH_STATE_INVALID")
        at = utcnow()
        with self.database.sessions.begin() as db:
            attempt = db.scalar(
                select(OAuthAuthorizationAttempt)
                .where(OAuthAuthorizationAttempt.state_digest == state_digest(state))
                .with_for_update()
            )
            if (
                attempt is None
                or attempt.provider != provider
                or attempt.user_id != current.user_id
                or attempt.session_id != current.session_id
                or attempt.consumed_at is not None
                or attempt.expires_at <= at
                or attempt.return_destination != "SETTINGS"
            ):
                raise OAuthConnectionError("OAUTH_STATE_INVALID")
            attempt.consumed_at = at
            return str(attempt.id)

    @staticmethod
    def _audit(db, action: str, actor_id, provider: str, reason: str | None = None):
        at = utcnow()
        db.add(
            AuditEvent(
                id=uuid.uuid4(),
                actor_type="USER" if actor_id else "SYSTEM",
                actor_id=actor_id,
                action=action,
                correlation_id=uuid.uuid4(),
                reason=reason,
                safe_metadata={"provider": provider},
                occurred_at=at,
            )
        )

    def complete(self, provider_slug: str, attempt_id: str, code: str, actor_id):
        provider = self._provider(provider_slug)
        scopes = REQUIRED_SCOPES[provider]
        redirect_uri = self._redirect_uri(provider)
        if not self.vault.is_configured() or not redirect_uri:
            raise OAuthConnectionError("OAUTH_NOT_CONFIGURED")
        tokens = self.gateway.exchange_code(provider, code, redirect_uri, scopes)
        if not tokens.access_token or scopes - set(tokens.scopes):
            raise OAuthConnectionError("OAUTH_REQUIRED_SCOPE_MISSING")
        identity = self.gateway.identify(provider, tokens.access_token)
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("provider_subject"), str)
            or not isinstance(identity.get("account_id"), str)
            or not ACCOUNT_ID.fullmatch(identity["account_id"])
        ):
            raise OAuthConnectionError("OAUTH_IDENTITY_INVALID")
        expected = (
            self.settings.linkedin_organization_id
            if provider == "LINKEDIN"
            else self.settings.instagram_professional_account_id
        )
        actual = identity["account_id"]
        if provider == "LINKEDIN" and expected:
            # The linked member URN and the configured Page are different identities;
            # Page role access is verified by the connector before Approval/execution.
            pass
        elif expected and actual != expected:
            raise OAuthConnectionError("OAUTH_ACCOUNT_MISMATCH")

        at = utcnow()
        attempt_uuid = uuid.UUID(attempt_id)
        with self.database.sessions.begin() as db:
            attempt = db.scalar(
                select(OAuthAuthorizationAttempt)
                .where(OAuthAuthorizationAttempt.id == attempt_uuid)
                .with_for_update()
            )
            if (
                attempt is None
                or attempt.provider != provider
                or attempt.user_id != actor_id
                or attempt.consumed_at is None
                or attempt.expires_at <= at
            ):
                raise OAuthConnectionError("OAUTH_STATE_INVALID")
            connection = db.scalar(
                select(ProviderConnection)
                .where(ProviderConnection.provider == provider)
                .with_for_update()
            )
            if connection is not None and connection.owner_user_id != actor_id:
                raise OAuthConnectionError("OAUTH_CONNECTION_OWNED_BY_ANOTHER_USER")
            if connection is None:
                connection = ProviderConnection(
                    id=uuid.uuid4(),
                    provider=provider,
                    owner_user_id=actor_id,
                    status="CONNECTED",
                    provider_subject=identity["provider_subject"],
                    account_id=actual,
                    account_name=identity.get("account_name"),
                    account_type=identity.get("account_type"),
                    granted_scopes=sorted(tokens.scopes),
                    connected_at=at,
                    created_at=at,
                    updated_at=at,
                )
                db.add(connection)
                db.flush()
            else:
                connection.status = "CONNECTED"
                connection.provider_subject = identity["provider_subject"]
                connection.account_id = actual
                connection.account_name = identity.get("account_name")
                connection.account_type = identity.get("account_type")
                connection.granted_scopes = sorted(tokens.scopes)
                connection.connected_at = at
                connection.access_expires_at = None
                connection.refresh_expires_at = None
                connection.last_refreshed_at = None
                connection.reauthorization_required_at = None
                connection.disconnected_at = None
                connection.updated_at = at
            connection.access_expires_at = at + timedelta(seconds=tokens.expires_in)
            connection.refresh_expires_at = (
                at + timedelta(seconds=tokens.refresh_expires_in)
                if tokens.refresh_expires_in
                else None
            )
            try:
                self.vault.store(db, connection.id, provider, tokens.secret_payload())
            except CredentialVaultError:
                raise OAuthConnectionError("OAUTH_VAULT_UNAVAILABLE") from None
            self._audit(db, "PROVIDER_CONNECTED", actor_id, provider)
            LOGGER.info("provider_connected provider=%s", provider)

    def metadata(self, provider_slug: str, actor_id) -> dict:
        provider = self._provider(provider_slug)
        with self.database.sessions() as db:
            connection = db.scalar(
                select(ProviderConnection).where(ProviderConnection.provider == provider)
            )
            if connection is None or connection.owner_user_id != actor_id:
                return {
                    "provider": provider_slug,
                    "status": "NOT_CONNECTED",
                    "account_name": None,
                    "account_id": None,
                    "account_type": None,
                    "granted_scopes": [],
                    "missing_scopes": sorted(REQUIRED_SCOPES[provider]),
                    "access_expires_at": None,
                    "vault_ready": self.vault.is_configured(),
                }
            status = connection.status
            if connection.access_expires_at and connection.access_expires_at <= utcnow():
                status = "REAUTH_REQUIRED"
            granted = set(connection.granted_scopes or [])
            return {
                "provider": provider_slug,
                "status": status,
                "account_name": connection.account_name,
                "account_id": connection.account_id,
                "account_type": connection.account_type,
                "granted_scopes": sorted(granted),
                "missing_scopes": sorted(REQUIRED_SCOPES[provider] - granted),
                "access_expires_at": (
                    connection.access_expires_at.isoformat()
                    if connection.access_expires_at
                    else None
                ),
                "vault_ready": self.vault.is_configured(),
            }

    def connection_metadata(self, actor_id) -> dict:
        return {slug: self.metadata(slug, actor_id) for slug in ("linkedin", "instagram")}

    def has_active_credentials(self, provider: str) -> bool:
        with self.database.sessions() as db:
            return (
                db.scalar(
                    select(ProviderConnection.id)
                    .join(
                        CredentialEnvelope,
                        CredentialEnvelope.connection_id == ProviderConnection.id,
                    )
                    .where(
                        ProviderConnection.provider == provider,
                        ProviderConnection.status == "CONNECTED",
                    )
                )
                is not None
            )

    def mark_reauthorization_required(self, provider: str) -> None:
        """Persist an authoritative provider 401 without retaining its response body."""
        if provider not in REQUIRED_SCOPES:
            return
        at = utcnow()
        with self.database.sessions.begin() as db:
            connection = db.scalar(
                select(ProviderConnection)
                .where(ProviderConnection.provider == provider)
                .with_for_update()
            )
            if connection is None or connection.status != "CONNECTED":
                return
            connection.status = "REAUTH_REQUIRED"
            connection.reauthorization_required_at = at
            connection.updated_at = at
            self._audit(db, "PROVIDER_REAUTH_REQUIRED", None, provider, "PROVIDER_401")
        LOGGER.info("provider_reauth_required provider=%s reason=PROVIDER_401", provider)

    def read_only_access_grant(self, provider: str) -> dict:
        """Decrypt a current grant without refresh or database mutation for smoke checks."""
        if provider not in REQUIRED_SCOPES:
            raise OAuthConnectionError("OAUTH_PROVIDER_UNSUPPORTED")
        at = utcnow()
        with self.database.sessions() as db:
            connection = db.scalar(
                select(ProviderConnection).where(ProviderConnection.provider == provider)
            )
            if connection is None or connection.status != "CONNECTED":
                raise OAuthConnectionError("OAUTH_REAUTH_REQUIRED")
            if connection.access_expires_at is None or connection.access_expires_at <= at:
                raise OAuthConnectionError("OAUTH_REAUTH_REQUIRED")
            envelope = db.scalar(
                select(CredentialEnvelope).where(CredentialEnvelope.connection_id == connection.id)
            )
            if envelope is None:
                raise OAuthConnectionError("OAUTH_CREDENTIAL_UNAVAILABLE")
            try:
                payload = self.vault.retrieve(provider, connection.id, envelope)
            except CredentialVaultError:
                raise OAuthConnectionError("OAUTH_VAULT_UNAVAILABLE") from None
            return {
                **payload,
                "provider_subject": connection.provider_subject,
                "account_id": connection.account_id,
                "account_name": connection.account_name,
                "account_type": connection.account_type,
                "scopes": list(connection.granted_scopes or []),
            }

    def access_grant(self, provider: str) -> dict:
        at = utcnow()
        result = None
        failure = None
        with self.database.sessions.begin() as db:
            connection = db.scalar(
                select(ProviderConnection)
                .where(ProviderConnection.provider == provider)
                .with_for_update()
            )
            if connection is None or connection.status != "CONNECTED":
                raise OAuthConnectionError("OAUTH_REAUTH_REQUIRED")
            envelope = db.scalar(
                select(CredentialEnvelope)
                .where(CredentialEnvelope.connection_id == connection.id)
                .with_for_update()
            )
            if envelope is None:
                raise OAuthConnectionError("OAUTH_CREDENTIAL_UNAVAILABLE")
            try:
                payload = self.vault.retrieve(provider, connection.id, envelope)
            except CredentialVaultError:
                raise OAuthConnectionError("OAUTH_VAULT_UNAVAILABLE") from None
            expiry = connection.access_expires_at
            refresh_window = (
                LINKEDIN_REFRESH_WINDOW if provider == "LINKEDIN" else INSTAGRAM_REFRESH_WINDOW
            )
            should_refresh = bool(expiry and expiry <= at + refresh_window)
            if provider == "LINKEDIN":
                should_refresh = should_refresh and bool(payload.get("refresh_token"))
                if connection.refresh_expires_at and connection.refresh_expires_at <= at:
                    should_refresh = False
            else:
                # Instagram's refresh path only accepts a still-valid long-lived token
                # which has been issued for at least 24 hours.
                should_refresh = bool(
                    expiry
                    and expiry > at
                    and expiry <= at + INSTAGRAM_REFRESH_WINDOW
                    and connection.connected_at <= at - timedelta(hours=24)
                )
            if should_refresh:
                try:
                    refreshed = self.gateway.refresh(provider, payload, connection.granted_scopes)
                except OAuthConnectionError as error:
                    if error.code in {"OAUTH_PROVIDER_REJECTED", "OAUTH_REAUTH_REQUIRED"}:
                        connection.status = "REAUTH_REQUIRED"
                        connection.reauthorization_required_at = at
                        connection.updated_at = at
                        self._audit(db, "PROVIDER_REAUTH_REQUIRED", None, provider, error.code)
                        failure = "OAUTH_REAUTH_REQUIRED"
                    else:
                        # A still-valid access token may serve until expiry; transient refresh
                        # failures do not make a usable token appear revoked.
                        if expiry is None or expiry <= at:
                            failure = "OAUTH_PROVIDER_UNAVAILABLE"
                else:
                    if set(refreshed.scopes) != set(connection.granted_scopes or []):
                        connection.status = "REAUTH_REQUIRED"
                        connection.reauthorization_required_at = at
                        connection.updated_at = at
                        self._audit(db, "PROVIDER_REAUTH_REQUIRED", None, provider, "SCOPE_CHANGED")
                        failure = "OAUTH_REAUTH_REQUIRED"
                    else:
                        new_payload = refreshed.secret_payload()
                        if provider == "LINKEDIN" and not refreshed.refresh_token:
                            if payload.get("refresh_token"):
                                new_payload["refresh_token"] = payload["refresh_token"]
                        self.vault.store(db, connection.id, provider, new_payload)
                        connection.access_expires_at = at + timedelta(seconds=refreshed.expires_in)
                        if refreshed.refresh_expires_in:
                            connection.refresh_expires_at = at + timedelta(
                                seconds=refreshed.refresh_expires_in
                            )
                        connection.last_refreshed_at = at
                        connection.updated_at = at
                        payload = new_payload
                        expiry = connection.access_expires_at
                        self._audit(db, "PROVIDER_CREDENTIAL_REFRESHED", None, provider)
                        LOGGER.info("provider_credential_refreshed provider=%s", provider)
            if expiry is None or expiry <= at:
                if failure is None:
                    connection.status = "REAUTH_REQUIRED"
                    connection.reauthorization_required_at = at
                    connection.updated_at = at
                    self._audit(db, "PROVIDER_REAUTH_REQUIRED", None, provider, "ACCESS_EXPIRED")
                    failure = "OAUTH_REAUTH_REQUIRED"
            if failure is None:
                result = {
                    **payload,
                    "provider_subject": connection.provider_subject,
                    "account_id": connection.account_id,
                    "account_name": connection.account_name,
                    "scopes": list(connection.granted_scopes or []),
                }
        if failure:
            raise OAuthConnectionError(failure)
        if result is None:
            raise OAuthConnectionError("OAUTH_CREDENTIAL_UNAVAILABLE")
        return result

    def disconnect(self, provider_slug: str, actor_id) -> dict:
        provider = self._provider(provider_slug)
        at = utcnow()
        payload = None
        with self.database.sessions.begin() as db:
            connection = db.scalar(
                select(ProviderConnection)
                .where(ProviderConnection.provider == provider)
                .with_for_update()
            )
            if connection is None or connection.owner_user_id != actor_id:
                return {"status": "NOT_CONNECTED", "remote_revocation": "NOT_ATTEMPTED"}
            envelope = db.scalar(
                select(CredentialEnvelope)
                .where(CredentialEnvelope.connection_id == connection.id)
                .with_for_update()
            )
            if envelope is not None:
                try:
                    payload = self.vault.retrieve(provider, connection.id, envelope)
                except CredentialVaultError:
                    payload = None
                db.delete(envelope)
            connection.status = "DISCONNECTED"
            connection.disconnected_at = connection.updated_at = at
        remote = "NOT_SUPPORTED"
        if payload:
            try:
                result = self.gateway.revoke(provider, payload)
                remote = "CONFIRMED" if result is True else "UNCONFIRMED"
            except Exception:
                remote = "UNCONFIRMED"
        with self.database.sessions.begin() as db:
            self._audit(
                db,
                "PROVIDER_DISCONNECTED",
                actor_id,
                provider,
                f"REMOTE_REVOCATION_{remote}",
            )
            if remote == "CONFIRMED":
                self._audit(db, "PROVIDER_REVOCATION_CONFIRMED", actor_id, provider)
        LOGGER.info("provider_disconnected provider=%s remote_revocation=%s", provider, remote)
        return {"status": "DISCONNECTED", "remote_revocation": remote}


oauth_router = APIRouter(prefix="/api/oauth")
connection_status_router = APIRouter(prefix="/api/settings")
callback_router = APIRouter()
CurrentOAuthSession = Annotated[auth.CurrentSession, Depends(auth.require_session)]


@connection_status_router.get("/oauth-connections")
def oauth_connection_status(request: Request, current: CurrentOAuthSession):
    return request.app.state.oauth_connections.connection_metadata(current.user_id)


@oauth_router.post("/{provider}/connect")
def connect_provider(provider: str, request: Request, current: CurrentOAuthSession):
    url = request.app.state.oauth_connections.start(provider, current)
    return {"authorization_url": url}


@oauth_router.post("/{provider}/disconnect")
def disconnect_provider(provider: str, request: Request, current: CurrentOAuthSession):
    return request.app.state.oauth_connections.disconnect(provider, current.user_id)


def _callback(provider: str, request: Request, current):
    connection_service = request.app.state.oauth_connections
    parameters = request.query_params
    if any(len(parameters.getlist(key)) != 1 for key in ("state",)):
        return "/settings?connection=failed"
    state = parameters.get("state", "")
    try:
        attempt_id = connection_service.consume(provider, state, current)
        if parameters.get("error"):
            return f"/settings?connection={provider}_denied"
        if len(parameters.getlist("code")) != 1:
            return "/settings?connection=failed"
        code = parameters.get("code", "")
        if not code or len(code) > 2048:
            return "/settings?connection=failed"
        connection_service.complete(provider, attempt_id, code, current.user_id)
        return f"/settings?connection={provider}_connected"
    except OAuthConnectionError:
        LOGGER.info("provider_oauth_rejected provider=%s", provider.upper())
        return f"/settings?connection={provider}_failed"
    except HTTPException:
        return f"/settings?connection={provider}_failed"


@callback_router.get("/auth/linkedin/callback")
def linkedin_callback(request: Request, current: CurrentOAuthSession):
    from fastapi.responses import RedirectResponse

    return RedirectResponse(_callback("linkedin", request, current), status_code=303)


@callback_router.get("/auth/instagram/callback")
def instagram_callback(request: Request, current: CurrentOAuthSession):
    from fastapi.responses import RedirectResponse

    return RedirectResponse(_callback("instagram", request, current), status_code=303)
