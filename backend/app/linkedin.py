"""Live LinkedIn Organization Posts API adapter with a vault-backed credential boundary."""

import hashlib
import io
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx2
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import (
    Campaign,
    CampaignVersion,
    CampaignVersionAsset,
    DestinationResourceBinding,
    GeneratedAsset,
)
from app.simulated_connectors import AdapterDeclaration, NormalizedResult

LOGGER = logging.getLogger("event_marketing.linkedin")
API_BASE = "https://api.linkedin.com/rest"
DESTINATION = "LINKEDIN_ORGANIZATION"
ADAPTER_REVISION = "linkedin-organization-live-v1"
MAX_COMMENTARY_LENGTH = 3000
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
REQUIRED_SCOPES = frozenset(
    {"openid", "profile", "r_organization_admin", "r_organization_social", "w_organization_social"}
)
# The Posts API lists CONTENT_ADMIN, while the current ACL role reference returns
# CONTENT_ADMINISTRATOR. Do not treat those strings as aliases without provider
# confirmation; use only values documented identically by both references.
POSTING_ROLES = frozenset({"ADMINISTRATOR", "DIRECT_SPONSORED_CONTENT_POSTER"})
POST_ID = re.compile(r"^urn:li:(?:share|ugcPost):[A-Za-z0-9_-]{1,160}$")
IMAGE_ID = re.compile(r"^urn:li:image:[A-Za-z0-9_-]{1,160}$")
ORGANIZATION_URN = re.compile(r"^urn:li:organization:[0-9]{1,30}$")
BLOCK_MESSAGES = {
    "LINKEDIN_CLIENT_ID_MISSING": "LinkedIn app client ID is not configured",
    "LINKEDIN_CLIENT_SECRET_MISSING": "LinkedIn app client secret is not configured",
    "LINKEDIN_REDIRECT_URI_MISSING": "LinkedIn sign-in callback is not configured",
    "LINKEDIN_ORGANIZATION_MISSING": "LinkedIn Page is not configured",
    "LINKEDIN_STANDARD_ACCESS_REQUIRED": (
        "Standard Community Management access is required for live publishing"
    ),
    "LINKEDIN_ACCESS_TIER_UNVERIFIED": (
        "LinkedIn Community Management access must be verified in the Developer Portal"
    ),
    "LINKEDIN_ACCOUNT_NOT_CONNECTED": "A LinkedIn member has not connected this application",
    "LINKEDIN_REQUIRED_SCOPE_MISSING": "The LinkedIn connection is missing a required permission",
    "LINKEDIN_ORGANIZATION_ROLE_MISSING": (
        "The connected member cannot publish to this LinkedIn Page"
    ),
    "LINKEDIN_ORGANIZATION_CHANGED": (
        "The approved LinkedIn Page no longer matches the configured Page"
    ),
    "LINKEDIN_POST_CONTENT_REQUIRED": "The approved LinkedIn post copy is missing",
    "LINKEDIN_POST_CONTENT_TOO_LONG": (
        "The approved LinkedIn post copy exceeds the supported limit; revise it before approval"
    ),
    "LINKEDIN_APPROVED_GRAPHIC_INVALID": "The approved campaign graphic could not be read exactly",
    "LINKEDIN_MEDIA_REPLACEMENT_UNSUPPORTED": (
        "LinkedIn cannot replace media on this published post through the supported update path"
    ),
    "LINKEDIN_REMOTE_POST_CHANGED": (
        "The LinkedIn post changed outside Event Marketing; review it before continuing"
    ),
    "LINKEDIN_IMAGE_NOT_READY": (
        "LinkedIn is still processing the approved graphic; no post was created"
    ),
    "LINKEDIN_UPLOAD_URL_INVALID": "LinkedIn returned an unsafe image upload address",
    "LINKEDIN_EXECUTION_CONTEXT_MISSING": "LinkedIn execution context is unavailable",
    "LINKEDIN_POST_ID_MISSING": "LinkedIn did not return a verifiable post ID",
}


class LinkedInError(Exception):
    """Sanitized API/provider failure; raw response bodies and credentials are discarded."""

    def __init__(
        self,
        category: str,
        *,
        status: int | None = None,
        code: str | None = None,
        may_have_applied: bool = False,
        details: dict | None = None,
    ):
        super().__init__(category)
        self.category = category
        self.status = status
        self.code = code if code and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else None
        self.may_have_applied = may_have_applied
        self.details = details or {}


@dataclass(frozen=True)
class LinkedInCredentialGrant:
    """A provider-issued access grant from an authorized member session."""

    access_token: str = field(repr=False)
    member_urn: str
    scopes: frozenset[str]
    connected: bool = True


class LinkedInCredentialProvider(Protocol):
    configured: bool

    def access_grant(self) -> LinkedInCredentialGrant: ...


class UnavailableLinkedInCredentials:
    """Production-safe default until encrypted OAuth credential storage is approved."""

    configured = False

    def access_grant(self) -> LinkedInCredentialGrant:
        raise LinkedInError("CONFIGURATION", code="LINKEDIN_ACCOUNT_NOT_CONNECTED")


class VaultLinkedInCredentials:
    """Resolve a LinkedIn grant only for live connector/readiness work."""

    def __init__(self, connections, fallback=None):
        self.connections = connections
        self.fallback = fallback

    @property
    def configured(self) -> bool:
        return self.connections.has_active_credentials("LINKEDIN") or bool(
            self.fallback and self.fallback.configured
        )

    def mark_reauthorization_required(self):
        self.connections.mark_reauthorization_required("LINKEDIN")

    def access_grant(self) -> LinkedInCredentialGrant:
        if not self.connections.has_active_credentials("LINKEDIN"):
            if self.fallback and self.fallback.configured:
                return self.fallback.access_grant()
            raise LinkedInError("CONFIGURATION", code="LINKEDIN_ACCOUNT_NOT_CONNECTED")
        try:
            value = self.connections.access_grant("LINKEDIN")
            return LinkedInCredentialGrant(
                access_token=value["access_token"],
                member_urn=value["account_id"],
                scopes=frozenset(value["scopes"]),
            )
        except Exception as error:
            code = getattr(error, "code", "LINKEDIN_TOKEN_REJECTED")
            mapped = (
                "LINKEDIN_ACCOUNT_NOT_CONNECTED"
                if code in {"OAUTH_REAUTH_REQUIRED", "OAUTH_CREDENTIAL_UNAVAILABLE"}
                else "LINKEDIN_CREDENTIAL_VAULT_UNAVAILABLE"
                if code == "OAUTH_VAULT_UNAVAILABLE"
                else "LINKEDIN_TOKEN_REJECTED"
            )
            raise LinkedInError("AUTHENTICATION", code=mapped) from None


def organization_urn(settings) -> str | None:
    value = getattr(settings, "linkedin_organization_id", "")
    if not value:
        return None
    return f"urn:li:organization:{value}"


def plan_context(settings) -> dict:
    urn = organization_urn(settings)
    return {
        "organization_id": settings.linkedin_organization_id or "",
        "organization_urn": urn or "",
        "ui_label": settings.linkedin_organization_name or "Event Marketing Page",
    }


def account_key(target: dict | str) -> str:
    urn = target if isinstance(target, str) else target.get("organization_urn")
    return f"organization:{urn or 'unconfigured'}"


def static_blockers(settings, credentials: LinkedInCredentialProvider) -> list[str]:
    checks = (
        (bool(settings.linkedin_client_id), "LINKEDIN_CLIENT_ID_MISSING"),
        (
            bool(settings.linkedin_client_secret.get_secret_value()),
            "LINKEDIN_CLIENT_SECRET_MISSING",
        ),
        (bool(settings.linkedin_redirect_uri), "LINKEDIN_REDIRECT_URI_MISSING"),
        (bool(organization_urn(settings)), "LINKEDIN_ORGANIZATION_MISSING"),
    )
    blockers = [reason for ready, reason in checks if not ready]
    tier = settings.linkedin_community_access_tier
    if tier == "UNVERIFIED":
        blockers.append("LINKEDIN_ACCESS_TIER_UNVERIFIED")
    elif tier != "STANDARD":
        blockers.append("LINKEDIN_STANDARD_ACCESS_REQUIRED")
    if not getattr(credentials, "configured", False):
        blockers.append("LINKEDIN_ACCOUNT_NOT_CONNECTED")
    return blockers


def _provider_error_code(response) -> str | None:
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("code") or body.get("serviceErrorCode") or body.get("status")
    return str(value) if isinstance(value, (int, str)) and len(str(value)) <= 64 else None


def _http_category(status: int) -> str:
    if status == 401:
        return "AUTHENTICATION"
    if status == 403:
        return "PERMISSION"
    if status == 404:
        return "NOT_FOUND"
    if status == 429:
        return "RATE_LIMIT"
    if 400 <= status < 500:
        return "VALIDATION"
    return "REMOTE_SERVER"


class LinkedInRestClient:
    """REST/Rest.li boundary; owns version headers, parsing and safe error mapping."""

    def __init__(
        self,
        settings,
        http_client=None,
        timeout: float = 25.0,
        on_authentication_failure=None,
    ):
        self.settings = settings
        self._owns_http = http_client is None
        self.http = http_client or httpx2.Client(timeout=timeout, follow_redirects=False)
        self.on_authentication_failure = on_authentication_failure

    def _mark_reauthorization_required(self):
        if self.on_authentication_failure is None:
            return
        try:
            self.on_authentication_failure()
        except Exception:
            LOGGER.warning("linkedin_reauthorization_status_update_failed")

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def _headers(self, token: str, *, json_body: bool = False) -> dict[str, str]:
        values = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Linkedin-Version": self.settings.linkedin_api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        if json_body:
            values["Content-Type"] = "application/json"
        return values

    def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        restli_method: str | None = None,
        expected: tuple[int, ...] = (200,),
    ):
        headers = self._headers(token, json_body=json_body is not None)
        if restli_method:
            headers["X-RestLi-Method"] = restli_method
        mutating = method.upper() not in {"GET", "HEAD"}
        try:
            response = self.http.request(
                method,
                path if path.startswith("https://") else f"{API_BASE}{path}",
                params=params,
                json=json_body,
                headers=headers,
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            raise LinkedInError("NETWORK", may_have_applied=False) from None
        except httpx2.HTTPError:
            raise LinkedInError("NETWORK", may_have_applied=mutating) from None
        if response.status_code not in expected:
            category = _http_category(response.status_code)
            if category == "AUTHENTICATION":
                self._mark_reauthorization_required()
            raise LinkedInError(
                category,
                status=response.status_code,
                code=_provider_error_code(response),
                may_have_applied=mutating
                and category
                not in {"AUTHENTICATION", "PERMISSION", "VALIDATION", "RATE_LIMIT", "NOT_FOUND"},
            )
        if response.status_code == 204 or not response.content:
            return response, None
        try:
            body = response.json()
        except Exception:
            raise LinkedInError(
                "UNKNOWN_OUTCOME", status=response.status_code, may_have_applied=mutating
            ) from None
        if not isinstance(body, dict):
            raise LinkedInError(
                "UNKNOWN_OUTCOME", status=response.status_code, may_have_applied=mutating
            )
        return response, body

    def upload_image(self, upload_url: str, token: str, data: bytes, mime_type: str):
        parsed = urlsplit(upload_url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or not parsed.hostname
            or not (parsed.hostname == "linkedin.com" or parsed.hostname.endswith(".linkedin.com"))
            or not parsed.path.startswith("/dms-uploads/")
        ):
            raise LinkedInError("VALIDATION", code="LINKEDIN_UPLOAD_URL_INVALID")
        # The current official upload guide specifies raw-byte PUT plus Authorization.
        # Do not forward Marketing-version headers, cookies, or any API-client headers.
        try:
            response = self.http.put(
                upload_url,
                content=data,
                headers={"Authorization": f"Bearer {token}"},
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            raise LinkedInError("NETWORK", may_have_applied=False) from None
        except httpx2.HTTPError:
            raise LinkedInError("NETWORK", may_have_applied=True) from None
        if response.status_code not in {200, 201, 204}:
            category = _http_category(response.status_code)
            if category == "AUTHENTICATION":
                self._mark_reauthorization_required()
            raise LinkedInError(
                category,
                status=response.status_code,
                code=_provider_error_code(response),
                may_have_applied=category
                not in {"AUTHENTICATION", "PERMISSION", "VALIDATION", "RATE_LIMIT"},
            )
        return response

    def organization_roles(self, token: str, member_urn: str) -> list[dict]:
        _response, body = self.request(
            "GET",
            "/organizationAcls",
            token,
            params={
                "q": "roleAssignee",
                "roleAssignee": member_urn,
                "state": "APPROVED",
                "count": 100,
                "start": 0,
            },
        )
        rows = body.get("elements") if isinstance(body, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise LinkedInError("UNKNOWN_OUTCOME")
        return rows

    def initialize_image(self, token: str, owner_urn: str) -> tuple[str, str]:
        _response, body = self.request(
            "POST",
            "/images",
            token,
            params={"action": "initializeUpload"},
            json_body={"initializeUploadRequest": {"owner": owner_urn}},
            expected=(200,),
        )
        value = body.get("value") if isinstance(body, dict) else None
        if not isinstance(value, dict):
            raise LinkedInError("UNKNOWN_OUTCOME", may_have_applied=True)
        image_id, upload_url = value.get("image"), value.get("uploadUrl")
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            raise LinkedInError("UNKNOWN_OUTCOME", may_have_applied=True)
        if not isinstance(upload_url, str):
            raise LinkedInError("UNKNOWN_OUTCOME", may_have_applied=True)
        return image_id, upload_url

    def image(self, token: str, image_id: str) -> dict:
        if not IMAGE_ID.fullmatch(image_id):
            raise LinkedInError("VALIDATION")
        _response, body = self.request("GET", f"/images/{quote(image_id, safe='')}", token)
        return body

    def post(self, token: str, post_id: str) -> dict:
        if not POST_ID.fullmatch(post_id):
            raise LinkedInError("VALIDATION")
        _response, body = self.request(
            "GET",
            f"/posts/{quote(post_id, safe='')}",
            token,
            params={"viewContext": "AUTHOR"},
        )
        return body

    def create_post(self, token: str, payload: dict) -> tuple[str | None, dict | None]:
        response, body = self.request("POST", "/posts", token, json_body=payload, expected=(201,))
        post_id = response.headers.get("x-restli-id")
        if post_id:
            from urllib.parse import unquote

            post_id = unquote(post_id)
        if post_id and not POST_ID.fullmatch(post_id):
            raise LinkedInError(
                "UNKNOWN_OUTCOME", status=response.status_code, may_have_applied=True
            )
        return post_id, body

    def update_commentary(self, token: str, post_id: str, commentary: str) -> None:
        if not POST_ID.fullmatch(post_id):
            raise LinkedInError("VALIDATION")
        self.request(
            "POST",
            f"/posts/{quote(post_id, safe='')}",
            token,
            json_body={"patch": {"$set": {"commentary": commentary}}},
            restli_method="PARTIAL_UPDATE",
            expected=(204,),
        )


class LinkedInRepository:
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
        """Return any LinkedIn Page binding so config drift cannot create a second post."""
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

    def bind_created(
        self, event_id, target: dict, post_id: str, execution_id, metadata: dict
    ) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            db.add(
                DestinationResourceBinding(
                    id=uuid.uuid4(),
                    destination_key=DESTINATION,
                    account_key=account_key(target),
                    event_id=uuid.UUID(str(event_id)),
                    external_id=post_id,
                    external_url=None,
                    # The Posts API create request asks LinkedIn to publish in one
                    # operation. The binding exists before readback to prevent a
                    # duplicate create; execution itself remains UNKNOWN until verified.
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

    def record(
        self, event_id, target: dict, execution_id, *, metadata: dict, verified=False
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
            row.resource_metadata = {**(row.resource_metadata or {}), **metadata}
            row.state = "PUBLISHED" if verified else row.state
            row.last_execution_id = uuid.UUID(str(execution_id))
            row.last_verified_at = now if verified else row.last_verified_at
            row.revision += 1
            row.updated_at = now

    def approved_graphic_bytes(self, campaign_version_id: str, graphic: dict) -> tuple[bytes, str]:
        from app.graphics_service import _read_asset

        try:
            version_uuid = uuid.UUID(str(campaign_version_id))
        except ValueError:
            raise LinkedInError("VALIDATION", code="LINKEDIN_APPROVED_GRAPHIC_INVALID") from None
        with self.sessions() as db:
            row = db.execute(
                select(CampaignVersionAsset, GeneratedAsset)
                .join(GeneratedAsset, GeneratedAsset.id == CampaignVersionAsset.asset_id)
                .where(
                    CampaignVersionAsset.version_id == version_uuid,
                    CampaignVersionAsset.role == "PRIMARY_GRAPHIC",
                )
            ).one_or_none()
            if row is None:
                raise LinkedInError("VALIDATION", code="LINKEDIN_APPROVED_GRAPHIC_INVALID")
            _manifest, asset = row
            if (
                str(asset.id) != graphic.get("asset_id")
                or asset.sha256 != graphic.get("sha256")
                or asset.mime_type not in {"image/png", "image/jpeg"}
            ):
                raise LinkedInError("VALIDATION", code="LINKEDIN_APPROVED_GRAPHIC_INVALID")
            try:
                data = _read_asset(self.settings, asset)
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    if (
                        image.format not in {"PNG", "JPEG"}
                        or image.size != (asset.width, asset.height)
                        or image.width * image.height > MAX_IMAGE_PIXELS
                        or len(data) > MAX_IMAGE_BYTES
                    ):
                        raise LinkedInError("VALIDATION", code="LINKEDIN_APPROVED_GRAPHIC_INVALID")
                    actual_mime = Image.MIME.get(image.format)
                if (
                    actual_mime != asset.mime_type
                    or hashlib.sha256(data).hexdigest() != asset.sha256
                ):
                    raise LinkedInError("VALIDATION", code="LINKEDIN_APPROVED_GRAPHIC_INVALID")
                return data, asset.mime_type
            except LinkedInError:
                raise
            except Exception:
                raise LinkedInError(
                    "VALIDATION", code="LINKEDIN_APPROVED_GRAPHIC_INVALID"
                ) from None


def render(plan_entry: dict) -> dict:
    if (
        plan_entry.get("destination_key") != DESTINATION
        or plan_entry.get("execution_kind") != "AUTO"
        or plan_entry.get("runtime_mode") != "LIVE"
    ):
        raise LinkedInError("CONFIGURATION", code="APPROVED_CONNECTOR_MODE_UNAVAILABLE")
    target = plan_entry.get("linkedin_organization")
    if (
        not isinstance(target, dict)
        or not isinstance(target.get("organization_urn"), str)
        or not ORGANIZATION_URN.fullmatch(target["organization_urn"])
        or not isinstance(target.get("organization_id"), str)
        or target["organization_urn"] != f"urn:li:organization:{target['organization_id']}"
    ):
        raise LinkedInError("CONFIGURATION", code="LINKEDIN_ORGANIZATION_MISSING")
    commentary = plan_entry.get("approved_content")
    if not isinstance(commentary, str) or not commentary.strip():
        raise LinkedInError("VALIDATION", code="LINKEDIN_POST_CONTENT_REQUIRED")
    if len(commentary) > MAX_COMMENTARY_LENGTH:
        raise LinkedInError("VALIDATION", code="LINKEDIN_POST_CONTENT_TOO_LONG")
    graphic = plan_entry.get("approved_graphic")
    if graphic is not None:
        if (
            not isinstance(graphic, dict)
            or not isinstance(graphic.get("asset_id"), str)
            or not isinstance(graphic.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", graphic["sha256"])
            or graphic.get("mime_type") not in {"image/png", "image/jpeg"}
        ):
            raise LinkedInError("VALIDATION", code="LINKEDIN_APPROVED_GRAPHIC_INVALID")
    return {
        "organization_urn": target["organization_urn"],
        "display_name": target.get("display_name"),
        "commentary": commentary,
        "event_title": plan_entry.get("event_title") or "Event Marketing event",
        "campaign_version_id": plan_entry.get("campaign_version_id"),
        "graphic": graphic,
    }


def _authorized_role(rows: list[dict], member_urn: str, org_urn: str) -> bool:
    return any(
        row.get("state") == "APPROVED"
        and row.get("roleAssignee") == member_urn
        and row.get("organizationTarget", row.get("organization")) == org_urn
        and row.get("role") in POSTING_ROLES
        for row in rows
    )


def live_readiness(
    settings, credentials: LinkedInCredentialProvider, client: LinkedInRestClient
) -> dict:
    result = {
        "mode": settings.linkedin_organization_execution_mode,
        "status": "NEEDS_SETUP",
        "reasons": static_blockers(settings, credentials),
        "organization_name": settings.linkedin_organization_name or None,
        "connected_member": None,
        "community_access_tier": settings.linkedin_community_access_tier,
        "checked_live": False,
        "connector_implemented": True,
    }
    tier_reasons = {
        "LINKEDIN_ACCESS_TIER_UNVERIFIED",
        "LINKEDIN_STANDARD_ACCESS_REQUIRED",
    }
    setup_reasons = [reason for reason in result["reasons"] if reason not in tier_reasons]
    if setup_reasons:
        LOGGER.info(
            "linkedin_readiness status=%s reason_count=%s", result["status"], len(setup_reasons)
        )
        result["reasons"] = setup_reasons
        return result
    try:
        grant = credentials.access_grant()
        if (
            not grant.connected
            or not grant.access_token
            or not grant.member_urn.startswith("urn:li:person:")
        ):
            raise LinkedInError("CONFIGURATION", code="LINKEDIN_ACCOUNT_NOT_CONNECTED")
        missing = sorted(REQUIRED_SCOPES - set(grant.scopes))
        if missing:
            result["reasons"] = ["LINKEDIN_REQUIRED_SCOPE_MISSING"]
            return result
        if not _authorized_role(
            client.organization_roles(grant.access_token, grant.member_urn),
            grant.member_urn,
            organization_urn(settings),
        ):
            result["reasons"] = ["LINKEDIN_ORGANIZATION_ROLE_MISSING"]
            result["status"] = "NEEDS_SETUP"
            result["checked_live"] = True
            return result
        if settings.linkedin_community_access_tier == "STANDARD":
            result.update(
                status="READY", reasons=[], checked_live=True, connected_member="Connected member"
            )
        else:
            reason = (
                "LINKEDIN_ACCESS_TIER_UNVERIFIED"
                if settings.linkedin_community_access_tier == "UNVERIFIED"
                else "LINKEDIN_STANDARD_ACCESS_REQUIRED"
            )
            result.update(
                status="NEEDS_SETUP",
                reasons=[reason],
                checked_live=True,
                connected_member="Connected member",
            )
    except LinkedInError as error:
        result["reasons"] = [error.code or _reason_for_category(error.category)]
        result["status"] = (
            "UNAVAILABLE"
            if error.category in {"NETWORK", "REMOTE_SERVER", "RATE_LIMIT", "UNKNOWN_OUTCOME"}
            else "NEEDS_SETUP"
        )
    except Exception:
        # Provider exceptions are intentionally sanitized at this boundary.
        result["reasons"] = ["LINKEDIN_READINESS_UNVERIFIED"]
        result["status"] = "UNAVAILABLE"
    LOGGER.info(
        "linkedin_readiness status=%s reason_count=%s", result["status"], len(result["reasons"])
    )
    return result


def _reason_for_category(category: str) -> str:
    return {
        "AUTHENTICATION": "LINKEDIN_TOKEN_REJECTED",
        "PERMISSION": "LINKEDIN_PERMISSION_DENIED",
        "RATE_LIMIT": "LINKEDIN_RATE_LIMITED",
        "NETWORK": "LINKEDIN_UNREACHABLE",
        "REMOTE_SERVER": "LINKEDIN_UNAVAILABLE",
        "UNKNOWN_OUTCOME": "LINKEDIN_READINESS_UNVERIFIED",
    }.get(category, "LINKEDIN_CONFIGURATION_INVALID")


class LinkedInLiveAdapter:
    accepts_context = True
    dynamic_readiness = True

    def __init__(
        self,
        settings,
        repository: LinkedInRepository,
        credentials: LinkedInCredentialProvider,
        client: LinkedInRestClient,
    ):
        self.settings = settings
        self.repository = repository
        self.credentials = credentials
        self.client = client
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

    def _grant_and_check_role(self, plan_entry: dict) -> LinkedInCredentialGrant:
        if self.blocker():
            raise HTTPException(409, self.blocker())
        try:
            listing = render(plan_entry)
            target = plan_entry["linkedin_organization"]
            if listing["organization_urn"] != organization_urn(self.settings):
                raise LinkedInError("CONFIGURATION", code="LINKEDIN_ORGANIZATION_CHANGED")
            if target.get("organization_id") != self.settings.linkedin_organization_id:
                raise LinkedInError("CONFIGURATION", code="LINKEDIN_ORGANIZATION_CHANGED")
            grant = self.credentials.access_grant()
            if (
                not grant.connected
                or not grant.access_token
                or not grant.member_urn.startswith("urn:li:person:")
            ):
                raise LinkedInError("CONFIGURATION", code="LINKEDIN_ACCOUNT_NOT_CONNECTED")
            if REQUIRED_SCOPES - set(grant.scopes):
                raise LinkedInError("PERMISSION", code="LINKEDIN_REQUIRED_SCOPE_MISSING")
            if not _authorized_role(
                self.client.organization_roles(grant.access_token, grant.member_urn),
                grant.member_urn,
                listing["organization_urn"],
            ):
                raise LinkedInError("PERMISSION", code="LINKEDIN_ORGANIZATION_ROLE_MISSING")
            return grant
        except LinkedInError as error:
            raise HTTPException(409, error.code or _reason_for_category(error.category)) from None

    def validate(self, plan_entry: dict, runtime_mode: str):
        if runtime_mode != "LIVE":
            raise HTTPException(409, "APPROVED_CONNECTOR_MODE_UNAVAILABLE")
        if self.blocker():
            raise HTTPException(409, self.blocker())
        try:
            listing = render(plan_entry)
        except LinkedInError as error:
            raise HTTPException(409, error.code or _reason_for_category(error.category)) from None
        existing = self.repository.binding_for_version(listing["campaign_version_id"])
        if existing:
            target = {"organization_urn": listing["organization_urn"]}
            if existing["account_key"] != account_key(target):
                raise HTTPException(409, "LINKEDIN_ORGANIZATION_CHANGED")
            metadata = existing.get("metadata") or {}
            if metadata.get("graphic_sha256") != self._graphic_hash(listing):
                raise HTTPException(409, "LINKEDIN_MEDIA_REPLACEMENT_UNSUPPORTED")
        self._grant_and_check_role(plan_entry)

    @staticmethod
    def _graphic_hash(listing: dict) -> str | None:
        return listing["graphic"].get("sha256") if listing["graphic"] else None

    def _metadata(self, listing: dict, image_id: str | None) -> dict:
        return {
            "organization_urn": listing["organization_urn"],
            "commentary": listing["commentary"],
            "graphic_sha256": self._graphic_hash(listing),
            "image_urn": image_id,
            "visibility": "PUBLIC",
            "feed_distribution": "MAIN_FEED",
        }

    def _matches(self, remote: dict, listing: dict, expected_image_id: str | None) -> bool:
        content = remote.get("content") or {}
        media = content.get("media") or {}
        actual_image_id = media.get("id") or None
        return bool(
            remote.get("author") == listing["organization_urn"]
            and remote.get("commentary") == listing["commentary"]
            and remote.get("visibility") == "PUBLIC"
            and (remote.get("distribution") or {}).get("feedDistribution") == "MAIN_FEED"
            and remote.get("lifecycleState") == "PUBLISHED"
            and actual_image_id == expected_image_id
        )

    def _read_image_available(self, token: str, image_id: str, owner_urn: str) -> bool:
        # Bounded read-only checks; never repeat an upload or post because processing is slow.
        for attempt in range(3):
            image = self.client.image(token, image_id)
            if image.get("id") == image_id and image.get("owner") == owner_urn:
                if image.get("status") == "AVAILABLE":
                    return True
                if image.get("status") in {"FAILED", "CLIENT_ERROR", "SERVER_ERROR", "INCOMPLETE"}:
                    return False
            if attempt < 2:
                time.sleep(0.1)
        return False

    def _load_approved_graphic(self, plan_entry: dict, listing: dict) -> tuple[bytes, str]:
        try:
            data, mime = self.repository.approved_graphic_bytes(
                str(plan_entry.get("campaign_version_id") or ""), listing["graphic"]
            )
            return data, mime
        except LinkedInError:
            raise

    def _image_for_create(
        self,
        plan_entry: dict,
        listing: dict,
        grant: LinkedInCredentialGrant,
        context: dict,
    ) -> tuple[str | None, dict | None]:
        graphic = listing["graphic"]
        if graphic is None:
            return None, None
        previous = context.get("previous_attempt_evidence") or {}
        reusable = previous.get("reusable_image")
        if (
            isinstance(reusable, dict)
            and reusable.get("sha256") == graphic.get("sha256")
            and reusable.get("organization_urn") == listing["organization_urn"]
            and isinstance(reusable.get("image_urn"), str)
            and IMAGE_ID.fullmatch(reusable["image_urn"])
        ):
            if self._read_image_available(
                grant.access_token, reusable["image_urn"], listing["organization_urn"]
            ):
                return reusable["image_urn"], reusable
        image_bytes, mime = self._load_approved_graphic(plan_entry, listing)
        image_id, upload_url = self.client.initialize_image(
            grant.access_token, listing["organization_urn"]
        )
        try:
            self.client.upload_image(upload_url, grant.access_token, image_bytes, mime)
        except LinkedInError as error:
            if error.may_have_applied:
                raise LinkedInError(
                    "UNKNOWN_OUTCOME",
                    status=error.status,
                    code="LINKEDIN_IMAGE_UPLOAD_UNCERTAIN",
                    may_have_applied=True,
                    details={
                        "stage": "IMAGE_UPLOAD",
                        "image_urn": image_id,
                        "graphic_sha256": graphic["sha256"],
                        "organization_urn": listing["organization_urn"],
                    },
                ) from None
            raise
        reusable = {
            "image_urn": image_id,
            "sha256": graphic["sha256"],
            "organization_urn": listing["organization_urn"],
        }
        if not self._read_image_available(
            grant.access_token, image_id, listing["organization_urn"]
        ):
            raise LinkedInError(
                "REMOTE_SERVER",
                code="LINKEDIN_IMAGE_NOT_READY",
                may_have_applied=False,
                details={
                    "stage": "IMAGE_UPLOAD",
                    "image_urn": image_id,
                    "graphic_sha256": graphic["sha256"],
                    "organization_urn": listing["organization_urn"],
                    "reusable_image": reusable,
                },
            )
        return image_id, reusable

    def _post_payload(self, listing: dict, image_id: str | None) -> dict:
        payload = {
            "author": listing["organization_urn"],
            "commentary": listing["commentary"],
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image_id:
            payload["content"] = {"media": {"id": image_id, "altText": listing["event_title"]}}
        return payload

    def _bind_and_verify(
        self,
        event_id,
        execution_id,
        listing: dict,
        post_id: str,
        metadata: dict,
        grant: LinkedInCredentialGrant,
        *,
        create: bool,
    ) -> NormalizedResult:
        target = {"organization_urn": listing["organization_urn"]}
        if create:
            try:
                self.repository.bind_created(event_id, target, post_id, execution_id, metadata)
            except IntegrityError:
                return self._uncertain(
                    post_id,
                    "The LinkedIn post may exist, but its identity could not be safely bound.",
                    stage="RESOURCE_BINDING",
                    evidence={"organization_urn": listing["organization_urn"]},
                )
        try:
            remote = self.client.post(grant.access_token, post_id)
        except LinkedInError as error:
            return self._error_result(error, "POST_READBACK", post_id)
        if not self._matches(remote, listing, metadata.get("image_urn")):
            return self._uncertain(
                post_id,
                "LinkedIn accepted the post, but its content could not be verified "
                "against the approved version.",
                stage="POST_READBACK",
                evidence={"organization_urn": listing["organization_urn"]},
            )
        self.repository.record(
            event_id,
            target,
            execution_id,
            metadata=metadata,
            verified=True,
        )
        return NormalizedResult(
            outcome="PUBLISHED",
            external_id=post_id,
            external_url=None,
            verification_status="VERIFIED",
            evidence={
                "adapter": ADAPTER_REVISION,
                "verification": "VERIFIED_LIVE",
                "organization_urn": listing["organization_urn"],
                "image_urn": metadata.get("image_urn"),
                "graphic_sha256": metadata.get("graphic_sha256"),
                "commentary_sha256": hashlib.sha256(listing["commentary"].encode()).hexdigest(),
            },
        )

    def execute(self, plan_entry: dict, idempotency_key: str, attempt_number: int, context=None):
        context = context or {}
        event_id = context.get("event_id")
        execution_id = context.get("execution_id")
        if not event_id or not execution_id:
            return self._blocked("LINKEDIN_EXECUTION_CONTEXT_MISSING", "PREFLIGHT")
        try:
            listing = render(plan_entry)
            grant = self._grant_and_check_role(plan_entry)
        except (HTTPException, LinkedInError) as error:
            code = str(error.detail) if isinstance(error, HTTPException) else error.code
            return self._blocked(code or "LINKEDIN_CONFIGURATION_INVALID", "PREFLIGHT")
        target = {"organization_urn": listing["organization_urn"]}
        event_binding = self.repository.binding_for_event(event_id)
        if event_binding and event_binding["account_key"] != account_key(target):
            return self._blocked("LINKEDIN_ORGANIZATION_CHANGED", "PREFLIGHT")
        binding = self.repository.binding(event_id, target)
        expected_hash = self._graphic_hash(listing)
        if binding:
            metadata = binding.get("metadata") or {}
            if metadata.get("organization_urn") != listing["organization_urn"]:
                return self._blocked("LINKEDIN_ORGANIZATION_CHANGED", "PREFLIGHT")
            if metadata.get("graphic_sha256") != expected_hash:
                return self._blocked("LINKEDIN_MEDIA_REPLACEMENT_UNSUPPORTED", "PREFLIGHT")
            post_id = binding.get("external_id")
            if not isinstance(post_id, str) or not POST_ID.fullmatch(post_id):
                return self._blocked("LINKEDIN_REMOTE_POST_CHANGED", "PREFLIGHT")
            try:
                remote = self.client.post(grant.access_token, post_id)
            except LinkedInError as error:
                return self._error_result(error, "POST_READ", post_id)
            current_image = metadata.get("image_urn")
            if not self._matches(
                remote, {**listing, "commentary": metadata.get("commentary") or ""}, current_image
            ):
                return self._blocked("LINKEDIN_REMOTE_POST_CHANGED", "PREFLIGHT")
            if remote.get("commentary") != listing["commentary"]:
                try:
                    self.client.update_commentary(
                        grant.access_token, post_id, listing["commentary"]
                    )
                except LinkedInError as error:
                    return self._error_result(error, "COMMENTARY_UPDATE", post_id)
            return self._bind_and_verify(
                event_id,
                execution_id,
                listing,
                post_id,
                self._metadata(listing, current_image),
                grant,
                create=False,
            )

        try:
            image_id, reusable_image = self._image_for_create(plan_entry, listing, grant, context)
        except LinkedInError as error:
            extra = {"organization_urn": listing["organization_urn"]}
            extra.update(error.details)
            if error.may_have_applied:
                return self._uncertain(
                    None,
                    "LinkedIn may still be processing the approved image; no post was created.",
                    stage=extra.pop("stage", "IMAGE_PREPARATION"),
                    evidence={**extra, "graphic_sha256": expected_hash},
                )
            if error.code == "LINKEDIN_IMAGE_NOT_READY":
                evidence = {
                    **extra,
                    "graphic_sha256": expected_hash,
                }
                return NormalizedResult(
                    outcome="NOT_CREATED",
                    human_error=BLOCK_MESSAGES[error.code],
                    error_category="REMOTE_SERVER",
                    retryable=True,
                    verification_status="NOT_CREATED",
                    evidence=evidence,
                )
            return self._error_result(error, "IMAGE_PREPARATION")
        metadata = self._metadata(listing, image_id)
        if reusable_image:
            metadata["reusable_image"] = reusable_image
        try:
            post_id, _body = self.client.create_post(
                grant.access_token, self._post_payload(listing, image_id)
            )
        except LinkedInError as error:
            return self._error_result(
                error,
                "POST_CREATE",
                evidence={
                    "organization_urn": listing["organization_urn"],
                    "image_urn": image_id,
                    "reusable_image": reusable_image,
                    "graphic_sha256": expected_hash,
                },
            )
        if not post_id:
            return self._uncertain(
                None,
                "LinkedIn may have created the post, but returned no verifiable post ID.",
                stage="POST_CREATE",
                evidence={
                    "organization_urn": listing["organization_urn"],
                    "image_urn": image_id,
                    "reusable_image": reusable_image,
                    "graphic_sha256": expected_hash,
                },
            )
        return self._bind_and_verify(
            event_id, execution_id, listing, post_id, metadata, grant, create=True
        )

    def verify(self, plan_entry: dict, idempotency_key: str, context=None) -> NormalizedResult:
        context = context or {}
        try:
            listing = render(plan_entry)
            grant = self._grant_and_check_role(plan_entry)
        except (HTTPException, LinkedInError) as error:
            code = str(error.detail) if isinstance(error, HTTPException) else error.code
            return NormalizedResult(
                outcome="VERIFICATION_RESULT",
                error_category="CONFIGURATION",
                human_error=BLOCK_MESSAGES.get(code, "LinkedIn verification is not available."),
                verification_status="INCONCLUSIVE",
                verification_finding="INCONCLUSIVE",
                evidence={"adapter": ADAPTER_REVISION, "stage": "READINESS"},
            )
        target = {"organization_urn": listing["organization_urn"]}
        binding = (
            self.repository.binding(context.get("event_id"), target)
            if context.get("event_id")
            else None
        )
        post_id = binding.get("external_id") if binding else context.get("external_id")
        previous_evidence = context.get("previous_attempt_evidence") or {}
        if previous_evidence.get("stage") == "IMAGE_UPLOAD" and not post_id:
            image_id = previous_evidence.get("image_urn")
            try:
                image = self.client.image(grant.access_token, image_id)
            except LinkedInError:
                return self._verification("INCONCLUSIVE", image_id, stage="IMAGE_UPLOAD")
            if (
                image.get("id") == image_id
                and image.get("owner") == listing["organization_urn"]
                and image.get("status") == "AVAILABLE"
            ):
                reusable = {
                    "image_urn": image_id,
                    "sha256": previous_evidence.get("graphic_sha256"),
                    "organization_urn": listing["organization_urn"],
                }
                return self._verification(
                    "CONFIRMED_ABSENT",
                    None,
                    stage="IMAGE_UPLOAD",
                    evidence={"reusable_image": reusable, "stage": "IMAGE_UPLOAD"},
                )
            return self._verification("INCONCLUSIVE", image_id, stage="IMAGE_UPLOAD")
        if not post_id:
            return self._verification("INCONCLUSIVE", stage="POST_CREATE")
        try:
            remote = self.client.post(grant.access_token, post_id)
        except LinkedInError as error:
            if error.category == "NOT_FOUND":
                return self._verification("CONFIRMED_ABSENT", post_id, stage="POST_READ")
            return self._verification("INCONCLUSIVE", post_id, stage="POST_READ")
        image_id = (binding.get("metadata") or {}).get("image_urn") if binding else None
        if image_id is None and listing["graphic"]:
            image_id = previous_evidence.get("image_urn") or (
                previous_evidence.get("reusable_image") or {}
            ).get("image_urn")
        if self._matches(remote, listing, image_id):
            if not binding and context.get("event_id") and context.get("execution_id"):
                try:
                    self.repository.bind_created(
                        context["event_id"],
                        target,
                        post_id,
                        context["execution_id"],
                        self._metadata(listing, image_id),
                    )
                except IntegrityError:
                    return self._verification("INCONCLUSIVE", post_id, stage="RESOURCE_BINDING")
            if context.get("execution_id"):
                self.repository.record(
                    context.get("event_id"),
                    target,
                    context["execution_id"],
                    metadata=self._metadata(listing, image_id),
                    verified=True,
                )
            return self._verification("FOUND_MATCHING", post_id, stage="POST_READ")
        if binding:
            old_meta = binding.get("metadata") or {}
            old_listing = {**listing, "commentary": old_meta.get("commentary") or ""}
            old_image = old_meta.get("image_urn")
            if self._matches(remote, old_listing, old_image):
                return self._verification("CONFIRMED_ABSENT", post_id, stage="POST_READ")
        return self._verification("INCONCLUSIVE", post_id, stage="POST_READ")

    def _verification(self, finding: str, post_id: str | None = None, *, stage: str, evidence=None):
        return NormalizedResult(
            outcome="VERIFICATION_RESULT",
            external_id=post_id,
            external_url=None,
            human_error=(
                "LinkedIn confirms the requested change did not take effect."
                if finding == "CONFIRMED_ABSENT"
                else "LinkedIn could not confirm the result."
                if finding == "INCONCLUSIVE"
                else None
            ),
            error_category="TRANSIENT_CONFIRMED_NO_EFFECT"
            if finding == "CONFIRMED_ABSENT"
            else None,
            retryable=finding == "CONFIRMED_ABSENT",
            verification_status="VERIFIED" if finding == "FOUND_MATCHING" else "INCONCLUSIVE",
            verification_finding=finding,
            evidence={
                "adapter": ADAPTER_REVISION,
                "verification": "VERIFIED_LIVE" if finding == "FOUND_MATCHING" else stage,
                "stage": stage,
                **(evidence or {}),
            },
        )

    def _blocked(self, code: str, stage: str) -> NormalizedResult:
        return NormalizedResult(
            outcome="BLOCKED",
            human_error=BLOCK_MESSAGES.get(code, "LinkedIn is not ready for this approved action."),
            error_category="CONFIGURATION"
            if "CONFIG" in code or "MISSING" in code
            else "VALIDATION",
            verification_status="NOT_REQUIRED",
            evidence={"adapter": ADAPTER_REVISION, "stage": stage, "reason_code": code},
        )

    def _uncertain(
        self, external_id, message: str, *, stage: str, evidence=None
    ) -> NormalizedResult:
        return NormalizedResult(
            outcome="UNCERTAIN",
            external_id=external_id,
            human_error=message,
            error_category="UNKNOWN_OUTCOME",
            retryable=False,
            verification_status="INCONCLUSIVE",
            evidence={"adapter": ADAPTER_REVISION, "stage": stage, **(evidence or {})},
        )

    def _error_result(self, error: LinkedInError, stage: str, external_id=None, evidence=None):
        category = error.category
        if error.may_have_applied:
            return self._uncertain(
                external_id,
                "LinkedIn may have accepted the request, but Event Marketing could not verify it.",
                stage=stage,
                evidence={"provider_category": category, **(evidence or {})},
            )
        retryable = category in {"RATE_LIMIT", "NETWORK", "REMOTE_SERVER"}
        if category in {"NETWORK", "REMOTE_SERVER"} and error.status is None:
            retryable = True
        reason_code = error.code or _reason_for_category(category)
        return NormalizedResult(
            outcome="NOT_CREATED",
            external_id=external_id,
            human_error=BLOCK_MESSAGES.get(reason_code, _safe_category_message(category)),
            error_category={"PERMISSION": "PERMISSION", "CONFIGURATION": "CONFIGURATION"}.get(
                category, category
            ),
            retryable=retryable,
            verification_status="NOT_CREATED",
            evidence={
                "adapter": ADAPTER_REVISION,
                "stage": stage,
                "provider_category": category,
                **(evidence or {}),
            },
        )


def _safe_category_message(category: str) -> str:
    return {
        "AUTHENTICATION": "LinkedIn needs to be connected again.",
        "PERMISSION": "LinkedIn access does not permit this Page action.",
        "VALIDATION": "LinkedIn rejected this approved content.",
        "RATE_LIMIT": "LinkedIn asked Event Marketing to slow down; try again later.",
        "NETWORK": (
            "LinkedIn could not be reached; the result is safe to retry only when confirmed."
        ),
        "REMOTE_SERVER": "LinkedIn is temporarily unavailable.",
        "CONFIGURATION": "LinkedIn needs setup before this action can run.",
        "UNKNOWN_OUTCOME": "LinkedIn's result could not be confirmed; do not retry yet.",
    }.get(category, "LinkedIn could not complete this action.")
