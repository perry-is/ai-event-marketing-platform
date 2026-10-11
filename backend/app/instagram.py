"""Instagram Login live adapter with a vault-backed credential boundary."""

import base64
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx2
from fastapi import APIRouter, HTTPException, Request, Response
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import is_external_https_url
from app.graphics_service import _read_asset
from app.models import (
    Campaign,
    CampaignVersion,
    CampaignVersionAsset,
    DestinationResourceBinding,
    GeneratedAsset,
)
from app.simulated_connectors import AdapterDeclaration, NormalizedResult

LOGGER = logging.getLogger("event_marketing.instagram")
DESTINATION = "INSTAGRAM"
API_HOST = "https://graph.instagram.com"
ADAPTER_REVISION = "instagram-professional-live-v1"
REQUIRED_SCOPES = frozenset({"instagram_business_basic", "instagram_business_content_publish"})
ACCOUNT_ID = re.compile(r"^[0-9]{1,32}$")
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
MEDIA_ID = PROVIDER_ID
MAX_CAPTION_LENGTH = 2200
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIN_IMAGE_EDGE = 320
MAX_IMAGE_EDGE = 1440
MIN_ASPECT_RATIO = 4 / 5
MAX_ASPECT_RATIO = 1.91
MEDIA_URL_TTL_SECONDS = 30 * 60
MAX_PROCESSING_POLLS = 5
PROCESSING_POLL_INTERVAL_SECONDS = 60

BLOCK_MESSAGES = {
    "INSTAGRAM_ACCOUNT_ID_MISSING": "Instagram Professional account is not configured.",
    "INSTAGRAM_ACCOUNT_NOT_CONNECTED": (
        "An Instagram Professional account has not connected this app."
    ),
    "INSTAGRAM_REQUIRED_SCOPE_MISSING": "Instagram is missing a required publishing permission.",
    "INSTAGRAM_ACCOUNT_MISMATCH": (
        "The approved Instagram account no longer matches the connected account."
    ),
    "INSTAGRAM_ACCOUNT_NOT_ELIGIBLE": "Instagram LIVE requires a Business or Creator account.",
    "INSTAGRAM_MEDIA_DELIVERY_NOT_CONFIGURED": (
        "Secure media delivery is not configured for Instagram."
    ),
    "INSTAGRAM_MEDIA_DELIVERY_REQUIRES_HTTPS": (
        "Secure media delivery needs a public HTTPS address."
    ),
    "INSTAGRAM_MEDIA_SIGNING_SECRET_MISSING": "Secure media delivery needs a signing secret.",
    "INSTAGRAM_CAPTION_REQUIRED": "The approved Instagram caption is missing.",
    "INSTAGRAM_CAPTION_TOO_LONG": (
        "The approved caption is too long for Instagram. Revise it before approval."
    ),
    "INSTAGRAM_GRAPHIC_REQUIRED": (
        "Instagram needs an approved campaign graphic before it can publish."
    ),
    "INSTAGRAM_APPROVED_GRAPHIC_INVALID": (
        "The approved campaign graphic could not be verified for Instagram."
    ),
    "INSTAGRAM_MEDIA_UNSUPPORTED": (
        "The approved campaign graphic does not meet Instagram image requirements."
    ),
    "INSTAGRAM_ALREADY_PUBLISHED_IMMUTABLE": (
        "This Instagram post is already published. Updating it automatically is not supported."
    ),
    "INSTAGRAM_TARGET_CHANGED": (
        "The approved Instagram account no longer matches the configured account."
    ),
    "INSTAGRAM_LIMIT_REACHED": (
        "Instagram’s current publishing limit has been reached. Try again later."
    ),
    "INSTAGRAM_PROCESSING_FAILED": "Instagram could not process the approved campaign graphic.",
    "INSTAGRAM_PROCESSING_UNRESOLVED": (
        "Instagram is still processing the campaign graphic. Check again before retrying."
    ),
}


class InstagramError(Exception):
    """Sanitized provider error; credentials and response bodies are never retained."""

    def __init__(
        self,
        category: str,
        *,
        status: int | None = None,
        code: str | None = None,
        may_have_applied: bool = False,
    ):
        super().__init__(category)
        self.category = category
        self.status = status
        self.code = code if code and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else None
        self.may_have_applied = may_have_applied


@dataclass(frozen=True)
class InstagramCredentialGrant:
    access_token: str = field(repr=False)
    account_id: str
    username: str | None
    scopes: frozenset[str]
    connected: bool = True


class InstagramCredentialProvider(Protocol):
    configured: bool

    def access_grant(self) -> InstagramCredentialGrant: ...


class UnavailableInstagramCredentials:
    """Production-safe default; real Instagram Login token persistence is deferred."""

    configured = False

    def access_grant(self) -> InstagramCredentialGrant:
        raise InstagramError("CONFIGURATION", code="INSTAGRAM_ACCOUNT_NOT_CONNECTED")


class VaultInstagramCredentials:
    """Resolve an Instagram grant only for live connector/readiness work."""

    def __init__(self, connections, fallback=None):
        self.connections = connections
        self.fallback = fallback

    @property
    def configured(self) -> bool:
        return self.connections.has_active_credentials("INSTAGRAM") or bool(
            self.fallback and self.fallback.configured
        )

    def mark_reauthorization_required(self):
        self.connections.mark_reauthorization_required("INSTAGRAM")

    def access_grant(self) -> InstagramCredentialGrant:
        if not self.connections.has_active_credentials("INSTAGRAM"):
            if self.fallback and self.fallback.configured:
                return self.fallback.access_grant()
            raise InstagramError("CONFIGURATION", code="INSTAGRAM_ACCOUNT_NOT_CONNECTED")
        try:
            value = self.connections.access_grant("INSTAGRAM")
            return InstagramCredentialGrant(
                access_token=value["access_token"],
                account_id=value["account_id"],
                username=value.get("account_name"),
                scopes=frozenset(value["scopes"]),
            )
        except Exception as error:
            code = getattr(error, "code", "INSTAGRAM_TOKEN_REJECTED")
            mapped = (
                "INSTAGRAM_ACCOUNT_NOT_CONNECTED"
                if code in {"OAUTH_REAUTH_REQUIRED", "OAUTH_CREDENTIAL_UNAVAILABLE"}
                else "INSTAGRAM_CREDENTIAL_VAULT_UNAVAILABLE"
                if code == "OAUTH_VAULT_UNAVAILABLE"
                else "INSTAGRAM_TOKEN_REJECTED"
            )
            raise InstagramError("AUTHENTICATION", code=mapped) from None


def plan_context(settings) -> dict:
    return {
        "account_id": settings.instagram_professional_account_id or "",
        "username": settings.instagram_username or "",
        "ui_label": (
            f"@{settings.instagram_username}"
            if settings.instagram_username
            else "Event Marketing Instagram"
        ),
    }


def account_key(target: dict | str) -> str:
    account_id = target if isinstance(target, str) else target.get("account_id")
    return f"professional-account:{account_id or 'unconfigured'}"


def static_blockers(settings, credentials: InstagramCredentialProvider) -> list[str]:
    blockers = []
    if not settings.instagram_professional_account_id:
        blockers.append("INSTAGRAM_ACCOUNT_ID_MISSING")
    if not getattr(credentials, "configured", False):
        blockers.append("INSTAGRAM_ACCOUNT_NOT_CONNECTED")
    base_url = settings.public_media_base_url
    if not base_url:
        blockers.append("INSTAGRAM_MEDIA_DELIVERY_NOT_CONFIGURED")
    elif settings.environment != "test" and not is_external_https_url(base_url):
        blockers.append("INSTAGRAM_MEDIA_DELIVERY_REQUIRES_HTTPS")
    if len(settings.media_delivery_signing_secret.get_secret_value()) < 32:
        blockers.append("INSTAGRAM_MEDIA_SIGNING_SECRET_MISSING")
    return blockers


class InstagramAPIClient:
    """Small Instagram Login Graph API boundary with normalized error handling."""

    def __init__(
        self,
        settings,
        http_client=None,
        timeout: float = 25.0,
        sleeper=time.sleep,
        on_authentication_failure=None,
    ):
        self.settings = settings
        self._owns_http = http_client is None
        self.http = http_client or httpx2.Client(timeout=timeout, follow_redirects=False)
        self.sleeper = sleeper
        self.on_authentication_failure = on_authentication_failure

    def _mark_reauthorization_required(self):
        if self.on_authentication_failure is None:
            return
        try:
            self.on_authentication_failure()
        except Exception:
            LOGGER.warning("instagram_reauthorization_status_update_failed")

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def _url(self, resource: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,220}", resource) or ".." in resource:
            raise InstagramError("VALIDATION")
        return f"{API_HOST}/{self.settings.instagram_graph_api_version}/{resource}"

    @staticmethod
    def _category(status: int) -> str:
        if status == 401:
            return "AUTHENTICATION"
        if status == 403:
            return "AUTHORIZATION"
        if status == 429:
            return "RATE_LIMIT"
        if 400 <= status < 500:
            return "VALIDATION"
        return "REMOTE_SERVER"

    @staticmethod
    def _provider_category(provider_code, status: int) -> str:
        by_code = {
            "190": "AUTHENTICATION",
            "10": "AUTHORIZATION",
            "4": "RATE_LIMIT",
            "17": "RATE_LIMIT",
            "32": "RATE_LIMIT",
            "2": "REMOTE_SERVER",
            "100": "VALIDATION",
        }
        if provider_code is not None and str(provider_code) in by_code:
            return by_code[str(provider_code)]
        return InstagramAPIClient._category(status) if status >= 400 else "VALIDATION"

    def request(
        self,
        method: str,
        resource: str,
        token: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        mutating: bool = False,
    ) -> dict:
        try:
            response = self.http.request(
                method,
                self._url(resource),
                params=params,
                data=data,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            raise InstagramError("NETWORK", may_have_applied=False) from None
        except httpx2.HTTPError:
            raise InstagramError("NETWORK", may_have_applied=mutating) from None
        try:
            body = response.json()
        except Exception:
            body = None
        provider_error = isinstance(body, dict) and isinstance(body.get("error"), dict)
        if not response.is_success or provider_error:
            raw_code = None
            if provider_error:
                raw = body["error"].get("code")
                if isinstance(raw, (int, str)) and len(str(raw)) <= 64:
                    raw_code = raw
            status = response.status_code
            category = self._provider_category(raw_code, status)
            if category == "AUTHENTICATION":
                self._mark_reauthorization_required()
            raise InstagramError(
                category,
                status=status,
                code=_reason_for_category(category),
                may_have_applied=mutating
                and category not in {"AUTHENTICATION", "AUTHORIZATION", "VALIDATION", "RATE_LIMIT"},
            )
        if not isinstance(body, dict):
            raise InstagramError("UNKNOWN_OUTCOME", may_have_applied=mutating)
        return body

    def account(self, token: str) -> dict:
        return self.request("GET", "me", token, params={"fields": "user_id,username,account_type"})

    def publishing_limit(self, token: str, account_id: str) -> dict:
        body = self.request(
            "GET",
            f"{quote(account_id, safe='')}/content_publishing_limit",
            token,
            params={"fields": "quota_usage,quota_total"},
        )
        rows = body.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        return body

    def create_container(self, token: str, account_id: str, *, caption: str, image_url: str) -> str:
        body = self.request(
            "POST",
            f"{quote(account_id, safe='')}/media",
            token,
            data={"image_url": image_url, "caption": caption},
            mutating=True,
        )
        value = body.get("id")
        if not isinstance(value, str) or not PROVIDER_ID.fullmatch(value):
            raise InstagramError("UNKNOWN_OUTCOME", may_have_applied=True)
        return value

    def container(self, token: str, container_id: str) -> dict:
        if not PROVIDER_ID.fullmatch(container_id):
            raise InstagramError("VALIDATION")
        return self.request(
            "GET", quote(container_id, safe=""), token, params={"fields": "id,status_code"}
        )

    def publish(self, token: str, account_id: str, container_id: str) -> str:
        body = self.request(
            "POST",
            f"{quote(account_id, safe='')}/media_publish",
            token,
            data={"creation_id": container_id},
            mutating=True,
        )
        value = body.get("id")
        if not isinstance(value, str) or not PROVIDER_ID.fullmatch(value):
            raise InstagramError("UNKNOWN_OUTCOME", may_have_applied=True)
        return value

    def media(self, token: str, media_id: str) -> dict:
        if not PROVIDER_ID.fullmatch(media_id):
            raise InstagramError("VALIDATION")
        return self.request(
            "GET",
            quote(media_id, safe=""),
            token,
            params={"fields": "id,caption,media_type,permalink,username,timestamp"},
        )


def _validate_target(target: dict) -> str:
    if not isinstance(target, dict) or not isinstance(target.get("account_id"), str):
        raise InstagramError("CONFIGURATION", code="INSTAGRAM_ACCOUNT_ID_MISSING")
    if not ACCOUNT_ID.fullmatch(target["account_id"]):
        raise InstagramError("CONFIGURATION", code="INSTAGRAM_ACCOUNT_ID_MISSING")
    return target["account_id"]


def render(plan_entry: dict) -> dict:
    if (
        plan_entry.get("destination_key") != DESTINATION
        or plan_entry.get("execution_kind") != "AUTO"
        or plan_entry.get("runtime_mode") != "LIVE"
    ):
        raise InstagramError("CONFIGURATION", code="APPROVED_CONNECTOR_MODE_UNAVAILABLE")
    target = plan_entry.get("instagram_account")
    account_id = _validate_target(target)
    caption = plan_entry.get("approved_content")
    if not isinstance(caption, str) or not caption.strip():
        raise InstagramError("VALIDATION", code="INSTAGRAM_CAPTION_REQUIRED")
    if len(caption) > MAX_CAPTION_LENGTH:
        raise InstagramError("VALIDATION", code="INSTAGRAM_CAPTION_TOO_LONG")
    graphic = plan_entry.get("approved_graphic")
    if not isinstance(graphic, dict):
        raise InstagramError("VALIDATION", code="INSTAGRAM_GRAPHIC_REQUIRED")
    if (
        not isinstance(graphic.get("asset_id"), str)
        or not re.fullmatch(r"[0-9a-f-]{36}", graphic.get("asset_id", ""), re.I)
        or not isinstance(graphic.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", graphic["sha256"])
        or graphic.get("mime_type") not in {"image/png", "image/jpeg", "image/webp"}
    ):
        raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID")
    version_id = plan_entry.get("campaign_version_id")
    if not isinstance(version_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", version_id, re.I):
        raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID")
    return {
        "account_id": account_id,
        "username": target.get("username") or None,
        "caption": caption,
        "graphic": {
            "asset_id": graphic["asset_id"],
            "sha256": graphic["sha256"],
            "mime_type": graphic["mime_type"],
        },
        "campaign_version_id": version_id,
    }


def _safe_jpeg(
    data: bytes,
    expected_sha256: str | None = None,
    expected_mime_type: str | None = None,
) -> tuple[bytes, int, int]:
    if not data or len(data) > 24 * 1024 * 1024:
        raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID")
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID")
    try:
        with Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "WEBP")) as image:
            formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
            if expected_mime_type and formats.get(expected_mime_type) != image.format:
                raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID")
            width, height = image.size
            if getattr(image, "n_frames", 1) != 1:
                raise InstagramError("VALIDATION", code="INSTAGRAM_MEDIA_UNSUPPORTED")
            image.load()
            if width * height > 25_000_000:
                raise InstagramError("VALIDATION", code="INSTAGRAM_MEDIA_UNSUPPORTED")
            ratio = width / height
            if (
                not MIN_IMAGE_EDGE <= width <= MAX_IMAGE_EDGE
                or not MIN_IMAGE_EDGE <= height <= MAX_IMAGE_EDGE
                or not MIN_ASPECT_RATIO <= ratio <= MAX_ASPECT_RATIO
            ):
                raise InstagramError("VALIDATION", code="INSTAGRAM_MEDIA_UNSUPPORTED")
            if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
                raise InstagramError("VALIDATION", code="INSTAGRAM_MEDIA_UNSUPPORTED")
            frame = image.convert("RGB")
            output = io.BytesIO()
            frame.save(output, format="JPEG", quality=100, subsampling=0, optimize=True)
    except InstagramError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID") from None
    jpeg = output.getvalue()
    if len(jpeg) > MAX_IMAGE_BYTES:
        raise InstagramError("VALIDATION", code="INSTAGRAM_MEDIA_UNSUPPORTED")
    return jpeg, width, height


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _signing_key(settings) -> bytes:
    value = settings.media_delivery_signing_secret.get_secret_value().encode("utf-8")
    if len(value) < 32:
        raise InstagramError("CONFIGURATION", code="INSTAGRAM_MEDIA_SIGNING_SECRET_MISSING")
    return value


def create_signed_media_url(
    settings, *, version_id: str, graphic: dict, now: int | None = None
) -> str:
    base = settings.public_media_base_url.rstrip("/")
    parsed = urlsplit(base)
    if not base or not parsed.hostname:
        raise InstagramError("CONFIGURATION", code="INSTAGRAM_MEDIA_DELIVERY_NOT_CONFIGURED")
    if settings.environment == "production" and parsed.scheme != "https":
        raise InstagramError("CONFIGURATION", code="INSTAGRAM_MEDIA_DELIVERY_REQUIRES_HTTPS")
    issued = int(now if now is not None else time.time())
    payload = {
        "version": version_id,
        "asset": graphic["asset_id"],
        "sha256": graphic["sha256"],
        "iat": issued,
        "exp": issued + MEDIA_URL_TTL_SECONDS,
        "nonce": secrets.token_hex(16),
    }
    encoded = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _b64(
        hmac.new(_signing_key(settings), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{base}/api/public/instagram-media/{encoded}.{signature}"


def _decode_signed_token(settings, token: str, now: int | None = None) -> dict:
    try:
        encoded, signature = token.split(".", 1)
        expected = _b64(
            hmac.new(_signing_key(settings), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
        current = int(now if now is not None else time.time())
        if (
            not isinstance(payload, dict)
            or not re.fullmatch(r"[0-9a-f-]{36}", str(payload.get("version", "")), re.I)
            or not re.fullmatch(r"[0-9a-f-]{36}", str(payload.get("asset", "")), re.I)
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("sha256", "")))
            or not isinstance(payload.get("iat"), int)
            or not isinstance(payload.get("exp"), int)
            or not isinstance(payload.get("nonce"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", payload["nonce"])
            or payload["iat"] > current + 30
            or payload["exp"] <= current
            or payload["exp"] - payload["iat"] > MEDIA_URL_TTL_SECONDS
        ):
            raise ValueError
        return payload
    except Exception:
        raise HTTPException(404, "MEDIA_NOT_FOUND") from None


def signed_media_bytes(
    sessions: sessionmaker, settings, token: str, now: int | None = None
) -> bytes:
    payload = _decode_signed_token(settings, token, now)
    try:
        version_id, asset_id = uuid.UUID(payload["version"]), uuid.UUID(payload["asset"])
    except ValueError:
        raise HTTPException(404, "MEDIA_NOT_FOUND") from None
    with sessions() as db:
        version = db.get(CampaignVersion, version_id)
        if version is None:
            raise HTTPException(404, "MEDIA_NOT_FOUND")
        pair = db.execute(
            select(CampaignVersionAsset, GeneratedAsset)
            .join(GeneratedAsset, GeneratedAsset.id == CampaignVersionAsset.asset_id)
            .where(
                CampaignVersionAsset.version_id == version_id,
                CampaignVersionAsset.asset_id == asset_id,
                CampaignVersionAsset.campaign_id == version.campaign_id,
                CampaignVersionAsset.role == "PRIMARY_GRAPHIC",
            )
        ).one_or_none()
        if pair is None:
            raise HTTPException(404, "MEDIA_NOT_FOUND")
        manifest, asset = pair
        if asset.asset_type != "CAMPAIGN_GRAPHIC" or asset.sha256 != payload["sha256"]:
            raise HTTPException(404, "MEDIA_NOT_FOUND")
        try:
            original = _read_asset(settings, asset)
            data, _width, _height = _safe_jpeg(original, asset.sha256, asset.mime_type)
        except (HTTPException, InstagramError):
            raise HTTPException(404, "MEDIA_NOT_FOUND") from None
        return data


public_media_router = APIRouter()


def _serve_signed_media(token: str, request: Request, *, head: bool = False):
    settings = request.app.state.settings
    sessions = request.app.state.database.sessions
    try:
        data = signed_media_bytes(sessions, settings, token)
    except HTTPException:
        raise
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": 'inline; filename="campaign.jpg"',
    }
    if head:
        headers["Content-Length"] = str(len(data))
        return Response(status_code=200, media_type="image/jpeg", headers=headers)
    return Response(content=data, media_type="image/jpeg", headers=headers)


@public_media_router.get(
    "/api/public/instagram-media/{token}", operation_id="get_instagram_signed_media"
)
def fetch_signed_media(token: str, request: Request):
    return _serve_signed_media(token, request)


@public_media_router.head("/api/public/instagram-media/{token}", include_in_schema=False)
def head_signed_media(token: str, request: Request):
    return _serve_signed_media(token, request, head=True)


class InstagramRepository:
    def __init__(self, sessions: sessionmaker, settings):
        self.sessions = sessions
        self.settings = settings

    def binding(self, event_id, target: dict) -> dict | None:
        with self.sessions() as db:
            row = db.scalar(
                select(DestinationResourceBinding).where(
                    DestinationResourceBinding.destination_key == DESTINATION,
                    DestinationResourceBinding.account_key == account_key(target),
                    DestinationResourceBinding.event_id == uuid.UUID(str(event_id)),
                )
            )
            if row is None:
                return None
            return {
                "external_id": row.external_id,
                "external_url": row.external_url,
                "state": row.state,
                "metadata": dict(row.resource_metadata or {}),
            }

    def binding_for_event(self, event_id) -> dict | None:
        with self.sessions() as db:
            row = db.scalar(
                select(DestinationResourceBinding).where(
                    DestinationResourceBinding.destination_key == DESTINATION,
                    DestinationResourceBinding.event_id == uuid.UUID(str(event_id)),
                )
            )
            if row is None:
                return None
            return {
                "account_key": row.account_key,
                "external_id": row.external_id,
                "external_url": row.external_url,
                "state": row.state,
                "metadata": dict(row.resource_metadata or {}),
            }

    def binding_for_version(self, version_id: str) -> dict | None:
        try:
            parsed = uuid.UUID(str(version_id))
        except ValueError:
            return None
        with self.sessions() as db:
            event_id = db.scalar(
                select(Campaign.event_id)
                .join(CampaignVersion, CampaignVersion.campaign_id == Campaign.id)
                .where(CampaignVersion.id == parsed)
            )
        return self.binding_for_event(event_id) if event_id else None

    def approved_graphic_bytes(
        self, version_id: str, graphic: dict
    ) -> tuple[bytes, bytes, int, int]:
        try:
            parsed_version = uuid.UUID(str(version_id))
            parsed_asset = uuid.UUID(str(graphic["asset_id"]))
        except (ValueError, KeyError):
            raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID") from None
        with self.sessions() as db:
            pair = db.execute(
                select(CampaignVersionAsset, GeneratedAsset)
                .join(GeneratedAsset, GeneratedAsset.id == CampaignVersionAsset.asset_id)
                .where(
                    CampaignVersionAsset.version_id == parsed_version,
                    CampaignVersionAsset.asset_id == parsed_asset,
                    CampaignVersionAsset.role == "PRIMARY_GRAPHIC",
                )
            ).one_or_none()
            if pair is None:
                raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID")
            _manifest, asset = pair
            if (
                asset.asset_type != "CAMPAIGN_GRAPHIC"
                or asset.sha256 != graphic.get("sha256")
                or asset.mime_type != graphic.get("mime_type")
            ):
                raise InstagramError("VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID")
            try:
                original = _read_asset(self.settings, asset)
                jpeg, width, height = _safe_jpeg(original, asset.sha256, asset.mime_type)
            except HTTPException:
                raise InstagramError(
                    "VALIDATION", code="INSTAGRAM_APPROVED_GRAPHIC_INVALID"
                ) from None
            return original, jpeg, width, height

    def bind_created(
        self,
        event_id,
        target: dict,
        media_id: str,
        permalink: str | None,
        execution_id,
        metadata: dict,
    ) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            db.add(
                DestinationResourceBinding(
                    id=uuid.uuid4(),
                    destination_key=DESTINATION,
                    account_key=account_key(target),
                    event_id=uuid.UUID(str(event_id)),
                    external_id=media_id,
                    external_url=permalink,
                    state="PUBLISHED",
                    resource_metadata=dict(metadata),
                    bound_by_execution_id=uuid.UUID(str(execution_id)),
                    last_execution_id=uuid.UUID(str(execution_id)),
                    revision=1,
                    first_bound_at=now,
                    last_verified_at=None,
                    updated_at=now,
                )
            )

    def record_verified(
        self, event_id, target: dict, execution_id, *, metadata: dict, permalink: str
    ) -> None:
        with self.sessions.begin() as db:
            row = db.scalar(
                select(DestinationResourceBinding)
                .where(
                    DestinationResourceBinding.destination_key == DESTINATION,
                    DestinationResourceBinding.account_key == account_key(target),
                    DestinationResourceBinding.event_id == uuid.UUID(str(event_id)),
                )
                .with_for_update()
            )
            if row is None:
                return
            now = datetime.now(UTC)
            row.external_url = permalink
            row.resource_metadata = {**(row.resource_metadata or {}), **metadata}
            row.state = "PUBLISHED"
            row.last_execution_id = uuid.UUID(str(execution_id))
            row.last_verified_at = now
            row.revision += 1
            row.updated_at = now


def _account_fields(grant: InstagramCredentialGrant, remote: dict, target_id: str) -> str | None:
    returned_id = remote.get("user_id")
    if not isinstance(returned_id, str) or not ACCOUNT_ID.fullmatch(returned_id):
        return "INSTAGRAM_READINESS_UNVERIFIED"
    if returned_id != target_id or grant.account_id != target_id:
        return "INSTAGRAM_ACCOUNT_MISMATCH"
    if remote.get("account_type") not in {"Business", "Media_Creator"}:
        return "INSTAGRAM_ACCOUNT_NOT_ELIGIBLE"
    if not isinstance(remote.get("username"), str) or not remote["username"].strip():
        return "INSTAGRAM_READINESS_UNVERIFIED"
    if REQUIRED_SCOPES - set(grant.scopes):
        return "INSTAGRAM_REQUIRED_SCOPE_MISSING"
    return None


def live_readiness(
    settings, credentials: InstagramCredentialProvider, client: InstagramAPIClient | None
) -> dict:
    result = {
        "mode": settings.instagram_execution_mode,
        "status": "NEEDS_SETUP",
        "reasons": static_blockers(settings, credentials),
        "username": settings.instagram_username or None,
        "checked_live": False,
        "connector_implemented": True,
        "account_eligible": False,
        "media_delivery_ready": bool(
            settings.public_media_base_url
            and (
                settings.environment == "test"
                or is_external_https_url(settings.public_media_base_url)
            )
            and len(settings.media_delivery_signing_secret.get_secret_value()) >= 32
        ),
    }
    if settings.instagram_execution_mode == "SIMULATED":
        result.update(status="SIMULATED", reasons=[], checked_live=False)
        return result
    if result["reasons"] or client is None:
        if client is None and not result["reasons"]:
            result["reasons"] = ["INSTAGRAM_READINESS_UNVERIFIED"]
        return result
    try:
        grant = credentials.access_grant()
        if not grant.connected or not grant.access_token:
            result["reasons"] = ["INSTAGRAM_ACCOUNT_NOT_CONNECTED"]
            return result
        account = client.account(grant.access_token)
        reason = _account_fields(grant, account, settings.instagram_professional_account_id)
        if reason:
            result["reasons"] = [reason]
            result["checked_live"] = True
            return result
        username = account.get("username")
        result["username"] = username if isinstance(username, str) else None
        result["account_eligible"] = True
        quota = client.publishing_limit(
            grant.access_token, settings.instagram_professional_account_id
        )
        used, total = quota.get("quota_usage"), quota.get("quota_total")
        if not isinstance(used, int) or not isinstance(total, int) or used < 0 or total <= 0:
            result["reasons"] = ["INSTAGRAM_READINESS_UNVERIFIED"]
            result["checked_live"] = True
            return result
        if used >= total:
            result["reasons"] = ["INSTAGRAM_LIMIT_REACHED"]
            result["checked_live"] = True
            return result
        result.update(status="READY", reasons=[], checked_live=True)
    except InstagramError as error:
        result["reasons"] = [error.code or _reason_for_category(error.category)]
        result["status"] = (
            "UNAVAILABLE"
            if error.category in {"NETWORK", "REMOTE_SERVER", "RATE_LIMIT", "UNKNOWN_OUTCOME"}
            else "NEEDS_SETUP"
        )
    except Exception:
        result["reasons"] = ["INSTAGRAM_READINESS_UNVERIFIED"]
        result["status"] = "UNAVAILABLE"
    LOGGER.info(
        "instagram_readiness status=%s reason_count=%s", result["status"], len(result["reasons"])
    )
    return result


def _reason_for_category(category: str) -> str:
    return {
        "AUTHENTICATION": "INSTAGRAM_TOKEN_REJECTED",
        "AUTHORIZATION": "INSTAGRAM_PERMISSION_DENIED",
        "VALIDATION": "INSTAGRAM_CONFIGURATION_INVALID",
        "RATE_LIMIT": "INSTAGRAM_RATE_LIMITED",
        "NETWORK": "INSTAGRAM_UNREACHABLE",
        "REMOTE_SERVER": "INSTAGRAM_UNAVAILABLE",
        "MEDIA_PROCESSING": "INSTAGRAM_PROCESSING_FAILED",
        "UNKNOWN_OUTCOME": "INSTAGRAM_READINESS_UNVERIFIED",
    }.get(category, "INSTAGRAM_CONFIGURATION_INVALID")


class InstagramLiveAdapter:
    accepts_context = True
    dynamic_readiness = True

    def __init__(
        self,
        settings,
        repository: InstagramRepository,
        credentials: InstagramCredentialProvider,
        client: InstagramAPIClient,
    ):
        self.settings, self.repository = settings, repository
        self.credentials, self.client = credentials, client
        self.declaration = AdapterDeclaration(
            destination_key=DESTINATION,
            execution_kind="AUTO",
            readiness_simulated="BLOCKED",
            readiness_live="UNVERIFIED",
            adapter_revision=ADAPTER_REVISION,
            can_verify=True,
        )

    @property
    def target(self) -> dict:
        return plan_context(self.settings)

    def close(self) -> None:
        self.client.close()
        close = getattr(self.credentials, "close", None)
        if close:
            close()

    def blocker(self) -> str | None:
        blockers = static_blockers(self.settings, self.credentials)
        return blockers[0] if blockers else None

    def _grant_and_check_account(self, target_id: str) -> InstagramCredentialGrant:
        if self.settings.instagram_professional_account_id != target_id:
            raise InstagramError("CONFIGURATION", code="INSTAGRAM_TARGET_CHANGED")
        if self.blocker():
            raise InstagramError("CONFIGURATION", code=self.blocker())
        grant = self.credentials.access_grant()
        if not grant.connected or not grant.access_token:
            raise InstagramError("CONFIGURATION", code="INSTAGRAM_ACCOUNT_NOT_CONNECTED")
        account = self.client.account(grant.access_token)
        reason = _account_fields(grant, account, target_id)
        if reason:
            raise InstagramError("AUTHORIZATION", code=reason)
        return InstagramCredentialGrant(
            access_token=grant.access_token,
            account_id=grant.account_id,
            username=account["username"],
            scopes=grant.scopes,
            connected=grant.connected,
        )

    def validate(self, plan_entry: dict, runtime_mode: str):
        if runtime_mode != "LIVE":
            raise HTTPException(409, "APPROVED_CONNECTOR_MODE_UNAVAILABLE")
        if self.blocker():
            raise HTTPException(409, self.blocker())
        try:
            listing = render(plan_entry)
            self._load_graphic(listing)
            binding = self.repository.binding_for_version(listing["campaign_version_id"])
            if binding is not None:
                expected_key = account_key({"account_id": listing["account_id"]})
                raise InstagramError(
                    "CONFIGURATION",
                    code=(
                        "INSTAGRAM_ALREADY_PUBLISHED_IMMUTABLE"
                        if binding["account_key"] == expected_key
                        else "INSTAGRAM_TARGET_CHANGED"
                    ),
                )
            grant = self._grant_and_check_account(listing["account_id"])
            quota = self.client.publishing_limit(grant.access_token, listing["account_id"])
            used, total = quota.get("quota_usage"), quota.get("quota_total")
            if not isinstance(used, int) or not isinstance(total, int) or used >= total:
                raise InstagramError("RATE_LIMIT", code="INSTAGRAM_LIMIT_REACHED")
        except InstagramError as error:
            raise HTTPException(409, error.code or _reason_for_category(error.category)) from None

    @staticmethod
    def _matches(
        remote: dict,
        listing: dict,
        username: str | None,
        expected_media_id: str | None = None,
    ) -> bool:
        permalink = remote.get("permalink")
        parsed = urlsplit(permalink) if isinstance(permalink, str) else None
        return bool(
            remote.get("id")
            and PROVIDER_ID.fullmatch(str(remote["id"]))
            and (expected_media_id is None or remote["id"] == expected_media_id)
            and remote.get("caption") == listing["caption"]
            and remote.get("media_type") == "IMAGE"
            and parsed is not None
            and parsed.scheme == "https"
            and parsed.hostname in {"instagram.com", "www.instagram.com"}
            and parsed.path.startswith("/p/")
            and parsed.username is None
            and parsed.password is None
            and bool(re.fullmatch(r"/p/[A-Za-z0-9_-]+/?", parsed.path))
            and isinstance(username, str)
            and remote.get("username") == username
        )

    def _error_result(
        self,
        error: InstagramError,
        stage: str,
        external_id: str | None = None,
        evidence: dict | None = None,
    ) -> NormalizedResult:
        detail = {"adapter": ADAPTER_REVISION, "stage": stage, **(evidence or {})}
        if error.may_have_applied or error.category == "UNKNOWN_OUTCOME":
            return NormalizedResult(
                outcome="UNCERTAIN",
                external_id=external_id,
                human_error=(
                    "Instagram may have accepted this action, but we couldn’t verify "
                    "the result. Do not retry until it is checked."
                ),
                error_category="UNKNOWN_OUTCOME",
                retryable=False,
                verification_status="INCONCLUSIVE",
                evidence=detail,
            )
        retryable = error.category in {"NETWORK", "RATE_LIMIT", "REMOTE_SERVER"}
        return NormalizedResult(
            outcome="NOT_CREATED",
            external_id=external_id,
            human_error={
                "AUTHENTICATION": "Instagram rejected the connection. Check the account setup.",
                "AUTHORIZATION": "Instagram did not allow publishing for this account.",
                "VALIDATION": "Instagram rejected these approved campaign details.",
                "RATE_LIMIT": "Instagram asked us to slow down. Try again later.",
                "NETWORK": "Instagram could not be reached. The action was not confirmed.",
                "REMOTE_SERVER": "Instagram had a problem. The action was not confirmed.",
            }.get(error.category, "Instagram could not complete this action."),
            error_category=error.category,
            retryable=retryable,
            verification_status="NOT_CREATED",
            evidence=detail,
        )

    def _uncertain(
        self, message: str, *, external_id: str | None, stage: str, evidence: dict
    ) -> NormalizedResult:
        return NormalizedResult(
            outcome="UNCERTAIN",
            external_id=external_id,
            human_error=message,
            error_category="UNKNOWN_OUTCOME",
            retryable=False,
            verification_status="INCONCLUSIVE",
            evidence={"adapter": ADAPTER_REVISION, "stage": stage, **evidence},
        )

    def _load_graphic(self, listing: dict) -> tuple[bytes, int, int]:
        try:
            _original, jpeg, width, height = self.repository.approved_graphic_bytes(
                listing["campaign_version_id"], listing["graphic"]
            )
            return jpeg, width, height
        except InstagramError:
            raise

    def _quota_available(self, token: str, account_id: str) -> bool:
        value = self.client.publishing_limit(token, account_id)
        used, total = value.get("quota_usage"), value.get("quota_total")
        if not isinstance(used, int) or not isinstance(total, int) or used < 0 or total <= 0:
            raise InstagramError("UNKNOWN_OUTCOME", code="INSTAGRAM_READINESS_UNVERIFIED")
        return used < total

    def _wait_until_finished(self, token: str, container_id: str) -> tuple[str, dict]:
        last = {}
        for index in range(MAX_PROCESSING_POLLS):
            last = self.client.container(token, container_id)
            if last.get("id") != container_id:
                raise InstagramError("UNKNOWN_OUTCOME")
            status = last.get("status_code")
            if status == "FINISHED":
                return status, last
            if status in {"ERROR", "EXPIRED"}:
                return status, last
            if status not in {"IN_PROGRESS", "PUBLISHED"}:
                raise InstagramError("UNKNOWN_OUTCOME")
            if index + 1 < MAX_PROCESSING_POLLS:
                self.client.sleeper(PROCESSING_POLL_INTERVAL_SECONDS)
        return str(last.get("status_code") or "UNKNOWN"), last

    def _bound(
        self,
        event_id,
        target: dict,
        execution_id,
        media_id: str,
        listing: dict,
        username: str | None,
    ) -> None:
        metadata = {
            "account_id": listing["account_id"],
            "username": username,
            "caption_sha256": hashlib.sha256(listing["caption"].encode("utf-8")).hexdigest(),
            "graphic_asset_id": listing["graphic"]["asset_id"],
            "graphic_sha256": listing["graphic"]["sha256"],
            "media_type": "IMAGE",
        }
        try:
            self.repository.bind_created(event_id, target, media_id, None, execution_id, metadata)
        except IntegrityError:
            # An already-bound resource or remote ID cannot be claimed twice.
            raise InstagramError(
                "UNKNOWN_OUTCOME", code="INSTAGRAM_RESOURCE_BINDING_CONFLICT"
            ) from None

    def _verify_and_record(
        self,
        event_id,
        execution_id,
        target: dict,
        listing: dict,
        grant: InstagramCredentialGrant,
        media_id: str,
        *,
        created: bool,
    ) -> NormalizedResult:
        try:
            remote = self.client.media(grant.access_token, media_id)
        except InstagramError:
            return self._uncertain(
                "Instagram may have published the post, but its readback could not be verified.",
                external_id=media_id,
                stage="MEDIA_READBACK",
                evidence={"account_id": listing["account_id"]},
            )
        if not self._matches(remote, listing, grant.username, media_id):
            return self._uncertain(
                "Instagram returned a post, but its content could not be verified "
                "against the approved version.",
                external_id=media_id,
                stage="MEDIA_READBACK",
                evidence={"account_id": listing["account_id"]},
            )
        binding = self.repository.binding(event_id, target)
        if binding is None or binding["external_id"] != media_id:
            return self._uncertain(
                "Instagram’s post is visible, but its identity could not be safely bound "
                "to this Event.",
                external_id=media_id,
                stage="RESOURCE_BINDING",
                evidence={"account_id": listing["account_id"]},
            )
        permalink = remote["permalink"]
        metadata = {
            "account_id": listing["account_id"],
            "username": grant.username,
            "caption_sha256": hashlib.sha256(listing["caption"].encode("utf-8")).hexdigest(),
            "graphic_asset_id": listing["graphic"]["asset_id"],
            "graphic_sha256": listing["graphic"]["sha256"],
            "media_type": "IMAGE",
        }
        if created:
            self.repository.record_verified(
                event_id, target, execution_id, metadata=metadata, permalink=permalink
            )
        return NormalizedResult(
            outcome="PUBLISHED",
            external_id=media_id,
            external_url=permalink,
            verification_status="VERIFIED",
            evidence={
                "adapter": ADAPTER_REVISION,
                "verification": "VERIFIED_LIVE",
                "account_id": listing["account_id"],
                "caption_sha256": metadata["caption_sha256"],
                "graphic_asset_id": listing["graphic"]["asset_id"],
                "graphic_sha256": listing["graphic"]["sha256"],
                "media_type": "IMAGE",
            },
        )

    def execute(self, plan_entry: dict, idempotency_key: str, attempt_number: int, context=None):
        context = context or {}
        event_id, execution_id = context.get("event_id"), context.get("execution_id")
        if not event_id or not execution_id:
            return self._blocked("INSTAGRAM_EXECUTION_CONTEXT_MISSING", "PREFLIGHT")
        try:
            listing = render(plan_entry)
            if self.blocker():
                return self._blocked(self.blocker(), "PREFLIGHT")
            # Validate exact approved asset and conversion BEFORE any provider mutation.
            self._load_graphic(listing)
            target = {"account_id": listing["account_id"]}
            current_target = plan_context(self.settings)
            if listing["account_id"] != current_target["account_id"]:
                return self._blocked("INSTAGRAM_TARGET_CHANGED", "PREFLIGHT")
            existing_event_binding = self.repository.binding_for_event(event_id)
            if existing_event_binding is not None:
                return self._blocked(
                    "INSTAGRAM_ALREADY_PUBLISHED_IMMUTABLE"
                    if existing_event_binding["account_key"] == account_key(target)
                    else "INSTAGRAM_TARGET_CHANGED",
                    "PREFLIGHT",
                )
            grant = self._grant_and_check_account(listing["account_id"])
            if not self._quota_available(grant.access_token, listing["account_id"]):
                return self._blocked("INSTAGRAM_LIMIT_REACHED", "PREFLIGHT")
            media_url = create_signed_media_url(
                self.settings,
                version_id=listing["campaign_version_id"],
                graphic=listing["graphic"],
            )
            context_evidence = {
                "adapter": ADAPTER_REVISION,
                "account_id": listing["account_id"],
                "campaign_version_id": listing["campaign_version_id"],
                "caption_sha256": hashlib.sha256(listing["caption"].encode("utf-8")).hexdigest(),
                "graphic_asset_id": listing["graphic"]["asset_id"],
                "graphic_sha256": listing["graphic"]["sha256"],
                "stage": "CONTAINER_CREATE_DISPATCHED",
            }
            try:
                container_id = self.client.create_container(
                    grant.access_token,
                    listing["account_id"],
                    caption=listing["caption"],
                    image_url=media_url,
                )
            except InstagramError as error:
                return self._error_result(error, "CONTAINER_CREATE", evidence=context_evidence)
            container_evidence = {
                **context_evidence,
                "stage": "CONTAINER_CREATED",
                "container_id": container_id,
            }
            status, _container = self._wait_until_finished(grant.access_token, container_id)
            if status in {"ERROR", "EXPIRED"}:
                return NormalizedResult(
                    outcome="NOT_CREATED",
                    external_id=container_id,
                    human_error=BLOCK_MESSAGES["INSTAGRAM_PROCESSING_FAILED"],
                    error_category="MEDIA_PROCESSING",
                    retryable=False,
                    verification_status="NOT_CREATED",
                    evidence={
                        **container_evidence,
                        "stage": "CONTAINER_FAILED",
                        "container_status": status,
                    },
                )
            if status != "FINISHED":
                return self._uncertain(
                    BLOCK_MESSAGES["INSTAGRAM_PROCESSING_UNRESOLVED"],
                    external_id=container_id,
                    stage="CONTAINER_PROCESSING",
                    evidence={**container_evidence, "container_status": status},
                )
            publish_evidence = {**container_evidence, "stage": "PUBLISH_DISPATCHED"}
            try:
                media_id = self.client.publish(
                    grant.access_token, listing["account_id"], container_id
                )
            except InstagramError as error:
                return self._error_result(error, "MEDIA_PUBLISH", container_id, publish_evidence)
            try:
                self._bound(event_id, target, execution_id, media_id, listing, grant.username)
            except InstagramError:
                return self._uncertain(
                    "Instagram may have published the post, but its identity could not be "
                    "safely saved.",
                    external_id=media_id,
                    stage="RESOURCE_BINDING",
                    evidence={"account_id": listing["account_id"]},
                )
            # A returned ID is bound before readback, preventing a duplicate on uncertainty.
            return self._verify_and_record(
                event_id, execution_id, target, listing, grant, media_id, created=True
            )
        except (InstagramError, HTTPException) as error:
            code = error.code if isinstance(error, InstagramError) else str(error.detail)
            return self._blocked(code or "INSTAGRAM_CONFIGURATION_INVALID", "PREFLIGHT")
        except Exception:
            LOGGER.warning("instagram_execution failed category=INTERNAL")
            return self._uncertain(
                "Instagram’s result could not be confirmed. Check the result before retrying.",
                external_id=None,
                stage="ADAPTER_EXCEPTION",
                evidence={},
            )

    def _blocked(self, code: str, stage: str) -> NormalizedResult:
        return NormalizedResult(
            outcome="BLOCKED",
            human_error=BLOCK_MESSAGES.get(
                code, "Instagram cannot publish this approved version yet."
            ),
            error_category="CONFIGURATION",
            retryable=False,
            verification_status="NOT_STARTED",
            evidence={"adapter": ADAPTER_REVISION, "stage": stage, "block_code": code},
        )

    def verify(self, plan_entry: dict, idempotency_key: str, context=None) -> NormalizedResult:
        context = context or {}
        try:
            listing = render(plan_entry)
            grant = self._grant_and_check_account(listing["account_id"])
            evidence = context.get("previous_attempt_evidence") or {}
            media_id = context.get("external_id")
            if (
                isinstance(media_id, str)
                and PROVIDER_ID.fullmatch(media_id)
                and evidence.get("stage")
                not in {"CONTAINER_CREATED", "CONTAINER_PROCESSING", "PUBLISH_DISPATCHED"}
            ):
                remote = self.client.media(grant.access_token, media_id)
                if self._matches(remote, listing, grant.username, media_id):
                    permalink = remote["permalink"]
                    event_id = context.get("event_id")
                    execution_id = context.get("execution_id")
                    binding = (
                        self.repository.binding(event_id, {"account_id": listing["account_id"]})
                        if event_id
                        else None
                    )
                    if binding is None or binding["external_id"] != media_id:
                        return NormalizedResult(
                            outcome="VERIFICATION_RESULT",
                            external_id=media_id,
                            verification_status="INCONCLUSIVE",
                            verification_finding="INCONCLUSIVE",
                            error_category="RESOURCE_BINDING_CONFLICT",
                            human_error=(
                                "Instagram’s post is visible, but its identity could not be "
                                "safely bound to this Event."
                            ),
                            evidence={"adapter": ADAPTER_REVISION, "stage": "RESOURCE_BINDING"},
                        )
                    if event_id and execution_id:
                        self.repository.record_verified(
                            event_id,
                            {"account_id": listing["account_id"]},
                            execution_id,
                            metadata={
                                "account_id": listing["account_id"],
                                "username": grant.username,
                                "caption_sha256": hashlib.sha256(
                                    listing["caption"].encode("utf-8")
                                ).hexdigest(),
                                "graphic_asset_id": listing["graphic"]["asset_id"],
                                "graphic_sha256": listing["graphic"]["sha256"],
                                "media_type": "IMAGE",
                            },
                            permalink=permalink,
                        )
                    return NormalizedResult(
                        outcome="VERIFICATION_RESULT",
                        external_id=media_id,
                        external_url=permalink,
                        verification_status="VERIFIED",
                        verification_finding="FOUND_MATCHING",
                        evidence={
                            "adapter": ADAPTER_REVISION,
                            "verification": "VERIFIED_LIVE",
                            "account_id": listing["account_id"],
                        },
                    )
                return NormalizedResult(
                    outcome="VERIFICATION_RESULT",
                    external_id=media_id,
                    verification_status="INCONCLUSIVE",
                    verification_finding="INCONCLUSIVE",
                    error_category="REMOTE_CHANGED",
                    human_error=(
                        "Instagram could not confirm that the saved post matches "
                        "this approved version."
                    ),
                    evidence={"adapter": ADAPTER_REVISION, "stage": "MEDIA_READBACK"},
                )
            container_id = evidence.get("container_id")
            if isinstance(container_id, str) and PROVIDER_ID.fullmatch(container_id):
                container = self.client.container(grant.access_token, container_id)
                status = container.get("status_code")
                if container.get("id") == container_id and status in {
                    "ERROR",
                    "EXPIRED",
                    "FINISHED",
                }:
                    # FINISHED proves a usable container, not that media_publish happened.
                    # It is safe to retry only when the prior attempt never dispatched publish.
                    if (
                        evidence.get("stage")
                        in {
                            "CONTAINER_CREATED",
                            "CONTAINER_PROCESSING",
                        }
                        and status == "FINISHED"
                    ):
                        return NormalizedResult(
                            outcome="VERIFICATION_RESULT",
                            external_id=container_id,
                            verification_status="NOT_CREATED",
                            verification_finding="CONFIRMED_ABSENT",
                            error_category="NOT_PUBLISHED",
                            human_error=(
                                "Instagram confirms the container is ready and no post was created."
                            ),
                            retryable=True,
                            evidence={
                                "adapter": ADAPTER_REVISION,
                                "stage": "CONTAINER_CREATED",
                                "container_id": container_id,
                            },
                        )
                    if status in {"ERROR", "EXPIRED"}:
                        return NormalizedResult(
                            outcome="VERIFICATION_RESULT",
                            external_id=container_id,
                            verification_status="NOT_CREATED",
                            verification_finding="CONFIRMED_ABSENT",
                            error_category="NOT_PUBLISHED",
                            human_error="Instagram confirms this container did not publish.",
                            retryable=True,
                            evidence={
                                "adapter": ADAPTER_REVISION,
                                "stage": "CONTAINER_FAILED",
                                "container_id": container_id,
                            },
                        )
            return NormalizedResult(
                outcome="VERIFICATION_RESULT",
                external_id=media_id if isinstance(media_id, str) else None,
                verification_status="INCONCLUSIVE",
                verification_finding="INCONCLUSIVE",
                error_category="AMBIGUOUS_OUTCOME",
                human_error=(
                    "Instagram’s result remains uncertain. Do not retry until it can be verified."
                ),
                retryable=False,
                evidence={
                    "adapter": ADAPTER_REVISION,
                    "stage": evidence.get("stage", "RECONCILIATION"),
                },
            )
        except InstagramError as error:
            return NormalizedResult(
                outcome="VERIFICATION_RESULT",
                external_id=context.get("external_id"),
                verification_status="INCONCLUSIVE",
                verification_finding="INCONCLUSIVE",
                error_category=error.category,
                human_error="Instagram’s result remains uncertain. Try checking again later.",
                retryable=False,
                evidence={"adapter": ADAPTER_REVISION, "stage": "RECONCILIATION"},
            )
