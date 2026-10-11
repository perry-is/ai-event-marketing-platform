import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import AppUser, ServerSession
from app.oidc import Identity


def now() -> datetime:
    return datetime.now(UTC)


def opaque_token() -> str:
    return secrets.token_urlsafe(32)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def valid_token(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"[A-Za-z0-9_-]{43}", value))


def session_row(token, kind, at, duration, **extra):
    return ServerSession(
        token_digest=digest(token),
        kind=kind,
        created_at=at,
        updated_at=at,
        last_seen_at=at,
        expires_at=at + duration,
        **extra,
    )


def create_login_flow(database):
    token, state, nonce, verifier = (opaque_token() for _ in range(4))
    at = now()
    with database.sessions.begin() as db:
        db.add(
            session_row(
                token,
                "LOGIN_FLOW",
                at,
                timedelta(minutes=10),
                private_data={"state_digest": digest(state), "nonce": nonce, "verifier": verifier},
            )
        )
    return token, state, nonce, verifier


def consume_login_flow(database, token: str | None, state: str | None):
    if not valid_token(token):
        raise HTTPException(400, "LOGIN_INVALID")
    with database.sessions.begin() as db:
        row = db.scalar(
            select(ServerSession)
            .where(ServerSession.token_digest == digest(token), ServerSession.kind == "LOGIN_FLOW")
            .with_for_update()
        )
        at = now()
        if row is None or row.revoked_at or row.expires_at <= at:
            raise HTTPException(400, "LOGIN_INVALID")
        data = row.private_data
        row.revoked_at = at
        row.updated_at = at
        row.private_data = None  # Consume and erase verifier/nonce before provider network IO.
    if (
        not valid_token(state)
        or not data
        or not secrets.compare_digest(digest(state), data["state_digest"])
    ):
        raise HTTPException(400, "LOGIN_INVALID")
    return data


def establish_session(database, settings, identity: Identity, previous_token: str | None):
    token = opaque_token()
    at = now()
    try:
        with database.sessions.begin() as db:
            user = db.scalar(
                select(AppUser)
                .where(AppUser.issuer == identity.issuer, AppUser.subject == identity.subject)
                .with_for_update()
            )
            if user is None:
                if identity.email not in settings.allowed_emails:
                    raise HTTPException(403, "ACCOUNT_NOT_ALLOWED")
                # Unique bootstrap entry prevents recycled email binding a second subject.
                user = AppUser(
                    issuer=identity.issuer,
                    subject=identity.subject,
                    allowlist_email=identity.email,
                    display_name=identity.display_name,
                    enabled=True,
                    created_at=at,
                    updated_at=at,
                    last_login_at=at,
                )
                db.add(user)
                db.flush()
            if not user.enabled or user.allowlist_email not in settings.allowed_emails:
                raise HTTPException(403, "ACCOUNT_NOT_ALLOWED")
            user.display_name = identity.display_name
            user.last_login_at = user.updated_at = at
            if valid_token(previous_token):
                old = db.scalar(
                    select(ServerSession)
                    .where(ServerSession.token_digest == digest(previous_token))
                    .with_for_update()
                )
                if old:
                    old.revoked_at = old.updated_at = at
            db.add(
                session_row(
                    token,
                    "APP_SESSION",
                    at,
                    timedelta(hours=12),
                    user_id=user.id,
                    authenticated_at=at,
                    private_data={"csrf_token": opaque_token()},
                )
            )
    except IntegrityError:
        raise HTTPException(403, "ACCOUNT_NOT_ALLOWED") from None
    return token


@dataclass
class CurrentSession:
    session_id: object
    user_id: object
    display_name: str
    csrf_token: str


def require_session(request: Request) -> CurrentSession:
    settings, database = request.app.state.settings, request.app.state.database
    token = request.cookies.get(settings.session_cookie)
    if not valid_token(token):
        raise HTTPException(401, "AUTH_REQUIRED")
    with database.sessions.begin() as db:
        row = db.scalar(
            select(ServerSession)
            .where(ServerSession.token_digest == digest(token), ServerSession.kind == "APP_SESSION")
            .with_for_update()
        )
        at = now()
        if row is None or row.revoked_at or row.expires_at <= at:
            raise HTTPException(401, "AUTH_REQUIRED")
        if row.last_seen_at + timedelta(hours=1) <= at:
            raise HTTPException(401, "AUTH_REQUIRED")
        user = db.get(AppUser, row.user_id)
        if not user or not user.enabled or user.allowlist_email not in settings.allowed_emails:
            raise HTTPException(401, "AUTH_REQUIRED")
        csrf = row.private_data["csrf_token"]
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if (
                request.headers.get("origin") != settings.app_origin
                or request.headers.get("sec-fetch-site") == "cross-site"
                or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf)
            ):
                raise HTTPException(403, "CSRF_REJECTED")
        row.last_seen_at = row.updated_at = at
        return CurrentSession(row.id, user.id, user.display_name, csrf)


def set_cookie(response, settings, name: str, value: str, seconds: int):
    response.set_cookie(
        name,
        value,
        max_age=seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_cookie(response, settings, name: str):
    response.delete_cookie(
        name, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
