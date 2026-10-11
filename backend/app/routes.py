import logging
from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from app import auth
from app.oidc import InvalidIdentity

logger = logging.getLogger("event_marketing")
public = APIRouter()
private = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])


class Health(BaseModel):
    status: str


class AuthStatus(BaseModel):
    login_available: bool


class Me(BaseModel):
    display_name: str
    csrf_token: str  # Antiforgery token, not an authentication credential/session ID.


class LoginLimiter:
    """Bounded single-process limit. No forwarded header is trusted as client identity."""

    def __init__(self):
        self.entries = OrderedDict()
        self.lock = Lock()

    def check(self, request):
        key = (request.client.host if request.client else "unknown", request.url.path)
        at = monotonic()
        with self.lock:
            start, count = self.entries.pop(key, (at, 0))
            if at - start > 60:
                start, count = at, 0
            self.entries[key] = (start, count + 1)
            while len(self.entries) > 1024:
                self.entries.popitem(last=False)
            if count >= 20:
                raise HTTPException(429, "LOGIN_RATE_LIMITED", headers={"Retry-After": "60"})


@public.get("/health/live", response_model=Health)
def live():
    return Health(status="alive")


@public.get("/health/ready", response_model=Health)
def ready(request: Request):
    with request.app.state.database.engine.connect() as db:
        db.execute(text("SELECT 1"))
        # Reachability alone cannot make an unmigrated database ready.
        db.execute(text("SELECT 1 FROM app_users LIMIT 1"))
        db.execute(text("SELECT 1 FROM sessions LIMIT 1"))
        db.execute(text("SELECT 1 FROM events LIMIT 1"))
        db.execute(text("SELECT 1 FROM source_materials LIMIT 1"))
        db.execute(text("SELECT 1 FROM approvals LIMIT 1"))
        db.execute(text("SELECT 1 FROM destination_executions LIMIT 1"))
        db.execute(text("SELECT 1 FROM execution_attempts LIMIT 1"))
        db.execute(text("SELECT 1 FROM audit_events LIMIT 1"))
        db.execute(text("SELECT 1 FROM assets LIMIT 1"))
        db.execute(text("SELECT 1 FROM campaign_version_assets LIMIT 1"))
        db.execute(text("SELECT 1 FROM provider_connections LIMIT 1"))
        db.execute(text("SELECT 1 FROM credential_envelopes LIMIT 1"))
        db.execute(text("SELECT 1 FROM oauth_authorization_attempts LIMIT 1"))
    return Health(status="ready")


@public.get("/auth/status", response_model=AuthStatus)
def status(request: Request):
    settings = request.app.state.settings
    return AuthStatus(login_available=settings.oidc_configured and bool(settings.allowed_emails))


@public.get("/auth/login")
def login(request: Request):
    request.app.state.login_limiter.check(request)
    settings = request.app.state.settings
    if not settings.oidc_configured or not settings.allowed_emails:
        raise HTTPException(503, "LOGIN_NOT_CONFIGURED")
    token, state, nonce, verifier = auth.create_login_flow(request.app.state.database)
    from fastapi.responses import RedirectResponse

    response = RedirectResponse(
        request.app.state.oidc.authorization_url(state, nonce, verifier), status_code=303
    )
    auth.set_cookie(response, settings, settings.flow_cookie, token, 600)
    return response


@public.get("/auth/callback")
async def callback(request: Request):
    request.app.state.login_limiter.check(request)
    settings = request.app.state.settings
    try:
        parameters = request.query_params
        if any(len(parameters.getlist(k)) != 1 for k in ("state", "code")):
            raise HTTPException(400, "LOGIN_INVALID")
        flow = auth.consume_login_flow(
            request.app.state.database,
            request.cookies.get(settings.flow_cookie),
            parameters.get("state"),
        )
        code = parameters.get("code", "")
        if "error" in parameters or not code or len(code) > 2048:
            raise HTTPException(400, "LOGIN_INVALID")
        identity = await request.app.state.oidc.exchange(code, flow["nonce"], flow["verifier"])
        token = auth.establish_session(
            request.app.state.database,
            settings,
            identity,
            request.cookies.get(settings.session_cookie),
        )
    except (InvalidIdentity, HTTPException):
        logger.info("login_rejected")
        from fastapi.responses import RedirectResponse

        response = RedirectResponse("/login?error=sign_in_failed", status_code=303)
        auth.clear_cookie(response, settings, settings.flow_cookie)
        return response
    from fastapi.responses import RedirectResponse

    response = RedirectResponse("/", status_code=303)
    auth.set_cookie(response, settings, settings.session_cookie, token, 12 * 3600)
    auth.clear_cookie(response, settings, settings.flow_cookie)
    logger.info("login_succeeded")
    return response


@private.get("/me", response_model=Me)
def me(current: Annotated[auth.CurrentSession, Depends(auth.require_session)]):
    return Me(display_name=current.display_name, csrf_token=current.csrf_token)


@private.post("/logout", status_code=204)
def logout(
    request: Request, current: Annotated[auth.CurrentSession, Depends(auth.require_session)]
):
    with request.app.state.database.sessions.begin() as db:
        from app.models import ServerSession

        row = db.get(ServerSession, current.session_id, with_for_update=True)
        row.revoked_at = row.updated_at = auth.now()
        row.private_data = None
    response = Response(status_code=204)
    auth.clear_cookie(
        response, request.app.state.settings, request.app.state.settings.session_cookie
    )
    logger.info("logout")
    return response
