"""Meetup GraphQL LIVE adapter for one approved Meetup Group.

This module deliberately supports only graphic-less, in-person events. Meetup's
official guide documents the photo placeholder and upload URL but does not
specify the binary upload contract. A graphic-bearing version is therefore
blocked before any Meetup mutation rather than published without its graphic.
"""

import hashlib
import html
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx2
from fastapi import HTTPException
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import DestinationResourceBinding
from app.simulated_connectors import AdapterDeclaration, NormalizedResult

LOGGER = logging.getLogger("event_marketing.meetup")
API_URL = "https://api.meetup.com/gql-ext"
TOKEN_URL = "https://secure.meetup.com/oauth2/access"
ADAPTER_REVISION = "meetup-live-v1"
DESTINATION = "MEETUP"
MAX_DESCRIPTION_LENGTH = 50_000
MEETUP_EVENT_URL = re.compile(
    r"^https://(?:www\.)?meetup\.com/[A-Za-z0-9_-]+/events/[A-Za-z0-9_-]+/?$",
    re.IGNORECASE,
)
US_ADDRESS = re.compile(
    r"^(?P<street>[^,]+),\s*(?P<city>[^,]+),\s*(?P<state>[A-Za-z]{2})"
    r"(?:\s+(?P<zip>\d{5}(?:-\d{4})?))?$"
)

SELF_READ = """query MeetupSelfReadiness { self { id name } }"""
NETWORK_READ = """query MeetupNetworkReadiness($urlname: ID!) {
  proNetwork(urlname: $urlname) { urlname name status }
}"""
GROUP_READ = """query MeetupGroupReadiness($urlname: String!) {
  groupByUrlname(urlname: $urlname) {
    id name urlname status
    proNetwork { urlname name }
    venues(first: 100) { edges { node { id name address city state country } } }
  }
}"""
EVENT_READ = """query MeetupEventRead($id: ID!) {
  event(id: $id) {
    id title eventUrl description dateTime duration eventType status
    group { id name urlname }
    venue { id name address city state country }
  }
}"""
EVENT_CREATE = """mutation MeetupEventCreate($input: CreateEventInput!) {
  createEvent(input: $input) {
    event { id title status eventUrl dateTime duration eventType group { urlname } }
    errors { message code field }
  }
}"""
EVENT_EDIT = """mutation MeetupEventEdit($input: EditEventInput!) {
  editEvent(input: $input) {
    event { id title status eventUrl dateTime duration eventType group { urlname } }
    errors { message code field }
  }
}"""


class MeetupError(Exception):
    """Sanitized connector error; raw provider bodies are never persisted or logged."""

    def __init__(
        self,
        category: str,
        *,
        code: str | None = None,
        status: int | None = None,
        may_have_applied: bool = False,
        external_id: str | None = None,
    ):
        super().__init__(category)
        self.category = category
        self.code = code if code and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else None
        self.status = status
        self.may_have_applied = may_have_applied
        self.external_id = external_id


class MeetupCredentialProvider(Protocol):
    def access_token(self) -> str: ...


def _configuration_blockers(settings) -> list[str]:
    required = (
        ("meetup_client_key", "MEETUP_CLIENT_KEY_MISSING"),
        ("meetup_authorized_member_id", "MEETUP_MEMBER_ID_MISSING"),
        ("meetup_signing_key_id", "MEETUP_SIGNING_KEY_ID_MISSING"),
        ("meetup_private_key_path", "MEETUP_PRIVATE_KEY_MISSING"),
        ("meetup_network_urlname", "MEETUP_NETWORK_MISSING"),
        ("meetup_group_urlname", "MEETUP_GROUP_MISSING"),
    )
    missing = [code for field, code in required if not getattr(settings, field, "")]
    key_path = getattr(settings, "meetup_private_key_path", "")
    if key_path:
        path = Path(key_path)
        if not path.is_file():
            missing.append("MEETUP_SIGNING_KEY_UNREADABLE")
        else:
            try:
                path.read_bytes()
            except OSError:
                missing.append("MEETUP_SIGNING_KEY_UNREADABLE")
    return missing


def member_fingerprint(member_id: str) -> str:
    return hashlib.sha256(f"meetup-member:{member_id}".encode()).hexdigest()


def plan_context(settings) -> dict:
    """Safe, stable target context frozen into a new approval plan."""
    member_id = getattr(settings, "meetup_authorized_member_id", "")
    return {
        "group_urlname": getattr(settings, "meetup_group_urlname", ""),
        "network_urlname": getattr(settings, "meetup_network_urlname", ""),
        "account_fingerprint": member_fingerprint(member_id) if member_id else None,
    }


def account_key(plan_target: dict) -> str:
    return f"member:{plan_target['account_fingerprint']}"


class MeetupJWTTokenProvider:
    """Short-lived, in-memory JWT owner credential exchange; tokens never persist."""

    def __init__(self, settings, http_client=None):
        self.settings = settings
        self.http = http_client or httpx2.Client(timeout=20.0, follow_redirects=False)
        self._owns_http = http_client is None
        self._cached_token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def _assertion(self) -> str:
        path = self.settings.meetup_private_key_path
        try:
            private_key = RSAKey.import_key(Path(path).read_bytes())
            now = int(time.time())
            return jwt.encode(
                {"alg": "RS256", "kid": self.settings.meetup_signing_key_id, "typ": "JWT"},
                {
                    "iss": self.settings.meetup_client_key,
                    "sub": self.settings.meetup_authorized_member_id,
                    "aud": "api.meetup.com",
                    "iat": now,
                    "exp": now + 120,
                },
                private_key,
                algorithms=["RS256"],
            )
        except Exception:
            raise MeetupError("CONFIGURATION", code="MEETUP_SIGNING_KEY_UNREADABLE") from None

    def access_token(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._cached_token and self._expires_at > now + 30:
                return self._cached_token
            blockers = _configuration_blockers(self.settings)
            if blockers:
                raise MeetupError("CONFIGURATION", code=blockers[0])
            assertion = self._assertion()
            try:
                response = self.http.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                    headers={"Accept": "application/json"},
                )
            except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
                raise MeetupError("NETWORK") from None
            except httpx2.HTTPError:
                raise MeetupError("NETWORK") from None
            if response.status_code == 401:
                raise MeetupError("AUTHENTICATION", status=401)
            if response.status_code == 403:
                raise MeetupError("PERMISSION", status=403)
            if response.status_code == 429:
                raise MeetupError("RATE_LIMIT", status=429)
            if response.status_code >= 500:
                raise MeetupError("REMOTE_SERVER", status=response.status_code)
            if response.status_code >= 400:
                raise MeetupError("AUTHENTICATION", status=response.status_code)
            try:
                body = response.json()
                token = body.get("access_token")
                expires = int(body.get("expires_in", 3600))
            except (ValueError, TypeError, AttributeError):
                raise MeetupError("UNKNOWN_OUTCOME", status=response.status_code) from None
            if not isinstance(token, str) or not token or expires < 1:
                raise MeetupError("UNKNOWN_OUTCOME", status=response.status_code)
            self._cached_token = token
            self._expires_at = now + min(expires, 3600)
            LOGGER.info("meetup_access_token_acquired")
            return token


class MeetupGraphQLClient:
    """Small GraphQL boundary that handles HTTP and GraphQL errors distinctly."""

    def __init__(self, credentials: MeetupCredentialProvider, http_client=None):
        self.credentials = credentials
        self.http = http_client or httpx2.Client(timeout=25.0, follow_redirects=False)
        self._owns_http = http_client is None

    def close(self) -> None:
        if self._owns_http:
            self.http.close()
        close = getattr(self.credentials, "close", None)
        if close:
            close()

    def execute(
        self, name: str, query: str, variables: dict | None = None, *, mutation=False
    ) -> dict:
        token = self.credentials.access_token()
        try:
            response = self.http.post(
                API_URL,
                json={"operationName": name, "query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            raise MeetupError("NETWORK", may_have_applied=False) from None
        except httpx2.HTTPError:
            raise MeetupError("NETWORK", may_have_applied=mutation) from None
        status = response.status_code
        if status == 401:
            raise MeetupError("AUTHENTICATION", status=status)
        if status == 403:
            raise MeetupError("PERMISSION", status=status)
        if status == 404:
            raise MeetupError("NOT_FOUND", status=status)
        if status == 429:
            raise MeetupError("RATE_LIMIT", status=status)
        if status >= 500:
            raise MeetupError("REMOTE_SERVER", status=status, may_have_applied=mutation)
        if status >= 400:
            raise MeetupError("VALIDATION", status=status)
        try:
            body = response.json()
        except ValueError:
            raise MeetupError("UNKNOWN_OUTCOME", status=status, may_have_applied=mutation) from None
        if not isinstance(body, dict):
            raise MeetupError("UNKNOWN_OUTCOME", status=status, may_have_applied=mutation)
        errors = body.get("errors")
        if errors:
            code = _graphql_error_code(errors)
            category = _graphql_error_category(errors)
            may_have_applied = mutation and category not in {
                "AUTHENTICATION",
                "PERMISSION",
                "RATE_LIMIT",
                "VALIDATION",
            }
            raise MeetupError(category, code=code, status=status, may_have_applied=may_have_applied)
        data = body.get("data")
        if not isinstance(data, dict):
            raise MeetupError("UNKNOWN_OUTCOME", status=status, may_have_applied=mutation)
        return data


def _graphql_error_code(errors) -> str | None:
    if not isinstance(errors, list):
        return None
    for error in errors:
        if isinstance(error, dict):
            code = (error.get("extensions") or {}).get("code") or error.get("code")
            if isinstance(code, str):
                return code
    return None


def _graphql_error_category(errors) -> str:
    code = (_graphql_error_code(errors) or "").upper()
    if "RATE_LIMIT" in code:
        return "RATE_LIMIT"
    if any(word in code for word in ("UNAUTHENTICATED", "AUTHENTICATION", "INVALID_TOKEN")):
        return "AUTHENTICATION"
    if any(word in code for word in ("FORBIDDEN", "PERMISSION", "UNAUTHORIZED")):
        return "PERMISSION"
    if code in {"BAD_USER_INPUT", "VALIDATION_ERROR", "GRAPHQL_VALIDATION_FAILED"}:
        return "VALIDATION"
    return "REMOTE_SERVER"


def _payload_errors(payload: dict | None) -> list:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return errors if isinstance(errors, list) else []


def _error_result(error: MeetupError, stage: str, external_id: str | None) -> NormalizedResult:
    if error.may_have_applied:
        message = (
            "Meetup may have created the event, but we couldn’t verify the result."
            if stage == "CREATE"
            else "Meetup may have applied the change, but we couldn’t verify the result."
        )
        return NormalizedResult(
            outcome="UNCERTAIN",
            external_id=error.external_id or external_id,
            human_error=message,
            error_category="AMBIGUOUS_OUTCOME",
            verification_status="INCONCLUSIVE",
            evidence={
                "adapter": ADAPTER_REVISION,
                "stage": stage,
                "provider_category": error.category,
                "provider_code": error.code,
                "external_id_known": bool(error.external_id or external_id),
            },
        )
    messages = {
        "AUTHENTICATION": "Meetup rejected the connection. Check the Meetup setup.",
        "PERMISSION": "Meetup did not allow this change for the connected account.",
        "NOT_FOUND": "The approved Meetup Group or Event could not be found.",
        "RATE_LIMIT": "Meetup asked us to slow down. Try again later.",
        "NETWORK": "Meetup could not be reached. Nothing was changed.",
        "REMOTE_SERVER": "Meetup had a problem. The Event was not confirmed.",
        "CONFIGURATION": "Meetup is not configured on the server.",
        "VALIDATION": "Meetup rejected these details. Review the Event information.",
    }
    return NormalizedResult(
        outcome="NOT_CREATED",
        external_id=None,
        human_error=messages.get(error.category, "Meetup could not complete this action."),
        error_category=error.category,
        retryable=error.category in {"NETWORK", "REMOTE_SERVER", "RATE_LIMIT"},
        verification_status="INCONCLUSIVE"
        if error.category in {"NETWORK", "REMOTE_SERVER"}
        else "VERIFIED",
        evidence={
            "adapter": ADAPTER_REVISION,
            "stage": stage,
            "provider_category": error.category,
            "provider_code": error.code,
            "external_id_known": external_id is not None,
        },
    )


class MeetupBlocked(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RenderedMeetupEvent:
    title: str
    description: str
    start_date_time: str
    duration_ms: int
    duration_create: str
    venue_name: str
    address: dict


def _html_description(text: str) -> str:
    paragraphs = [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    return "".join(
        "<p>" + "<br>".join(html.escape(line) for line in part.split("\n")) + "</p>"
        for part in paragraphs
    )


def _canonical_address(value: str) -> dict | None:
    match = US_ADDRESS.fullmatch(" ".join(value.split()))
    if not match:
        return None
    return {
        "street": _normalize_location(match.group("street")),
        "city": _normalize_location(match.group("city")),
        "state": match.group("state").upper(),
    }


def _normalize_location(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def _iso_duration(duration_ms: int) -> str:
    seconds = duration_ms // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if hours:
        parts.append(f"{hours}H")
    if minutes:
        parts.append(f"{minutes}M")
    if seconds or not parts:
        parts.append(f"{seconds}S")
    return "PT" + "".join(parts)


def render(plan_entry: dict) -> RenderedMeetupEvent:
    # New destination plans freeze only the necessary Event facts under `meetup_event`.
    facts = plan_entry.get("meetup_event")
    if not isinstance(facts, dict):
        raise MeetupBlocked("MEETUP_LISTING_MISSING")
    title = str(facts.get("name") or plan_entry.get("event_title") or "").strip()
    text = plan_entry.get("approved_content")
    timezone = facts.get("timezone")
    start_raw, end_raw = facts.get("starts_at"), facts.get("ends_at")
    if not title:
        raise MeetupBlocked("MEETUP_TITLE_REQUIRED")
    if len(title) > 80:
        raise MeetupBlocked("MEETUP_TITLE_TOO_LONG")
    if not isinstance(text, str) or not text.strip():
        raise MeetupBlocked("MEETUP_DESCRIPTION_REQUIRED")
    description = _html_description(text)
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise MeetupBlocked("MEETUP_DESCRIPTION_TOO_LONG")
    if not start_raw or not end_raw or not timezone:
        raise MeetupBlocked("MEETUP_SCHEDULE_REQUIRED")
    try:
        zone = ZoneInfo(timezone)
        start = datetime.fromisoformat(str(start_raw))
        end = datetime.fromisoformat(str(end_raw))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("schedule values must include offsets")
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        duration_ms = int((end - start).total_seconds() * 1000)
        start_local = start.astimezone(zone).isoformat(timespec="seconds")
    except (ValueError, TypeError, KeyError):
        raise MeetupBlocked("MEETUP_SCHEDULE_INVALID") from None
    if duration_ms <= 0 or duration_ms % 1000:
        raise MeetupBlocked("MEETUP_DURATION_REQUIRED")
    delivery = facts.get("delivery")
    if delivery != "IN_PERSON":
        raise MeetupBlocked("MEETUP_EVENT_TYPE_UNVERIFIED")
    if facts.get("price_kind") != "FREE":
        raise MeetupBlocked(
            "MEETUP_PAID_EVENT_UNSUPPORTED"
            if facts.get("price_kind") == "PAID"
            else "MEETUP_PRICE_UNKNOWN"
        )
    venue_name = str(facts.get("venue_name") or "").strip()
    address = _canonical_address(str(facts.get("address") or ""))
    if not venue_name or address is None:
        raise MeetupBlocked("MEETUP_LOCATION_REQUIRED")
    return RenderedMeetupEvent(
        title=title,
        description=description,
        start_date_time=start_local,
        duration_ms=duration_ms,
        duration_create=_iso_duration(duration_ms),
        venue_name=venue_name,
        address=address,
    )


def _remote_address_matches(remote: dict, listing: RenderedMeetupEvent) -> bool:
    remote_address = _normalize_location(remote.get("address"))
    return bool(
        remote.get("name")
        and _normalize_location(remote.get("name")) == _normalize_location(listing.venue_name)
        and remote_address == listing.address["street"]
        and _normalize_location(remote.get("city")) == listing.address["city"]
        and (remote.get("state") or "").upper() == listing.address["state"]
        and _normalize_location(remote.get("country")) in {"us", "usa", "united states"}
    )


def _venue_id(group: dict, listing: RenderedMeetupEvent) -> str:
    edges = (group.get("venues") or {}).get("edges") or []
    matches = [
        edge.get("node")
        for edge in edges
        if isinstance(edge, dict)
        and isinstance(edge.get("node"), dict)
        and _remote_address_matches(edge["node"], listing)
    ]
    if len(matches) != 1 or not matches[0].get("id"):
        raise MeetupBlocked("MEETUP_VENUE_UNAVAILABLE")
    return str(matches[0]["id"])


def _parse_remote_datetime(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _duration_milliseconds(value) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    if value.isdigit():
        return int(value)
    match = re.fullmatch(
        r"PT(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?",
        value,
    )
    if not match:
        return None
    seconds = (
        float(match.group("h") or 0) * 3600
        + float(match.group("m") or 0) * 60
        + float(match.group("s") or 0)
    )
    return int(seconds * 1000)


class MeetupRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def binding(self, event_id, account: str) -> dict | None:
        with self.sessions() as db:
            row = db.scalar(
                select(DestinationResourceBinding).where(
                    DestinationResourceBinding.destination_key == DESTINATION,
                    DestinationResourceBinding.account_key == account,
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

    def bind_created(
        self, event_id, account: str, external_id: str, execution_id, group_urlname: str
    ):
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            db.add(
                DestinationResourceBinding(
                    id=uuid.uuid4(),
                    destination_key=DESTINATION,
                    account_key=account,
                    event_id=uuid.UUID(str(event_id)),
                    external_id=external_id,
                    external_url=None,
                    state="DRAFT",
                    resource_metadata={"group_urlname": group_urlname},
                    bound_by_execution_id=uuid.UUID(str(execution_id)),
                    last_execution_id=uuid.UUID(str(execution_id)),
                    revision=1,
                    first_bound_at=now,
                    last_verified_at=None,
                    updated_at=now,
                )
            )

    def record(
        self, event_id, account: str, execution_id, *, state=None, external_url=None, verified=False
    ):
        with self.sessions.begin() as db:
            row = db.scalar(
                select(DestinationResourceBinding)
                .where(
                    DestinationResourceBinding.destination_key == DESTINATION,
                    DestinationResourceBinding.account_key == account,
                    DestinationResourceBinding.event_id == uuid.UUID(str(event_id)),
                )
                .with_for_update()
            )
            if row is None:
                return
            now = datetime.now(UTC)
            if state:
                row.state = state
            if external_url:
                row.external_url = external_url
            if verified:
                row.last_verified_at = now
            row.last_execution_id = uuid.UUID(str(execution_id))
            row.revision += 1
            row.updated_at = now


def _target(plan_entry: dict) -> dict:
    target = plan_entry.get("meetup")
    if not isinstance(target, dict):
        raise MeetupBlocked("MEETUP_TARGET_MISSING")
    if not target.get("group_urlname") or not target.get("account_fingerprint"):
        raise MeetupBlocked("MEETUP_TARGET_MISSING")
    return target


def _group_and_network(client: MeetupGraphQLClient, target: dict) -> dict:
    network_data = client.execute(
        "MeetupNetworkReadiness", NETWORK_READ, {"urlname": target["network_urlname"]}
    )
    network = network_data.get("proNetwork")
    if (
        not isinstance(network, dict)
        or network.get("urlname") != target["network_urlname"]
        or network.get("status") not in {None, "ACTIVE"}
    ):
        raise MeetupBlocked("MEETUP_NETWORK_UNAVAILABLE")
    group_data = client.execute(
        "MeetupGroupReadiness", GROUP_READ, {"urlname": target["group_urlname"]}
    )
    group = group_data.get("groupByUrlname")
    if not isinstance(group, dict) or group.get("urlname") != target["group_urlname"]:
        raise MeetupBlocked("MEETUP_GROUP_UNAVAILABLE")
    if group.get("status") not in {None, "ACTIVE"}:
        raise MeetupBlocked("MEETUP_GROUP_UNAVAILABLE")
    linked_network = group.get("proNetwork") or {}
    if linked_network.get("urlname") != target["network_urlname"]:
        raise MeetupBlocked("MEETUP_GROUP_NETWORK_MISMATCH")
    return group


def live_readiness(settings, client: MeetupGraphQLClient | None) -> dict:
    blockers = _configuration_blockers(settings)
    result = {
        "mode": settings.meetup_execution_mode,
        "status": "NEEDS_SETUP" if blockers else "READY",
        "reasons": blockers,
        "member_name": None,
        "network_name": None,
        "group_name": None,
        "checked_live": False,
        "photo_capability": "DEFERRED",
    }
    if blockers:
        return result
    if client is None:
        result.update(status="UNAVAILABLE", reasons=["MEETUP_UNREACHABLE"])
        return result
    target = plan_context(settings)
    try:
        data = client.execute("MeetupSelfReadiness", SELF_READ)
        member = data.get("self")
        if (
            not isinstance(member, dict)
            or str(member.get("id")) != settings.meetup_authorized_member_id
        ):
            result.update(status="UNAVAILABLE", reasons=["MEETUP_ACCOUNT_MISMATCH"])
            return result
        result["member_name"] = str(member.get("name") or "")[:120] or None
        group = _group_and_network(client, target)
        result["group_name"] = str(group.get("name") or "")[:120] or None
        network = group.get("proNetwork") or {}
        result["network_name"] = str((network or {}).get("name") or "")[:120] or None
        result["checked_live"] = True
    except MeetupBlocked as blocked:
        result.update(status="NEEDS_SETUP", reasons=[blocked.code])
    except MeetupError as error:
        reason = {
            "AUTHENTICATION": "MEETUP_TOKEN_REJECTED",
            "PERMISSION": "MEETUP_PERMISSION_DENIED",
            "RATE_LIMIT": "MEETUP_RATE_LIMITED",
            "CONFIGURATION": error.code or "MEETUP_CONFIGURATION_INVALID",
        }.get(error.category, "MEETUP_UNREACHABLE")
        result.update(status="UNAVAILABLE", reasons=[reason])
    LOGGER.info("meetup_readiness status=%s reasons=%s", result["status"], result["reasons"])
    return result


class MeetupLiveAdapter:
    accepts_context = True

    def __init__(self, settings, repository: MeetupRepository, client: MeetupGraphQLClient):
        self.settings = settings
        self.repository = repository
        self.client = client
        blockers = _configuration_blockers(settings)
        self.declaration = AdapterDeclaration(
            destination_key=DESTINATION,
            execution_kind="AUTO",
            readiness_simulated="BLOCKED",
            readiness_live="READY" if not blockers else "BLOCKED",
            adapter_revision=ADAPTER_REVISION,
            can_verify=True,
        )

    @property
    def account(self):
        member_id = self.settings.meetup_authorized_member_id
        return {"account_fingerprint": member_fingerprint(member_id)} if member_id else None

    @property
    def target(self):
        return plan_context(self.settings)

    def close(self):
        self.client.close()

    def blocker(self):
        blockers = _configuration_blockers(self.settings)
        return blockers[0] if blockers else None

    def validate(self, plan_entry: dict, runtime_mode: str):
        if runtime_mode != "LIVE":
            raise HTTPException(409, "APPROVED_CONNECTOR_MODE_UNAVAILABLE")
        if (
            plan_entry.get("destination_key") != DESTINATION
            or plan_entry.get("execution_kind") != "AUTO"
        ):
            raise HTTPException(409, "ADAPTER_DESTINATION_MISMATCH")
        if self.blocker():
            raise HTTPException(409, self.blocker())
        try:
            target = _target(plan_entry)
            if target["account_fingerprint"] != member_fingerprint(
                self.settings.meetup_authorized_member_id
            ):
                raise MeetupBlocked("MEETUP_ACCOUNT_CHANGED")
            if plan_entry.get("approved_graphic"):
                raise MeetupBlocked("MEETUP_GRAPHIC_UPLOAD_UNVERIFIED")
            render(plan_entry)
        except MeetupBlocked as blocked:
            raise HTTPException(409, blocked.code) from None

    def render(self, plan_entry: dict) -> dict:
        self.validate(plan_entry, "LIVE")
        listing = render(plan_entry)
        target = _target(plan_entry)
        return {
            "group_urlname": target["group_urlname"],
            "network_urlname": target["network_urlname"],
            "title": listing.title,
            "description": listing.description,
            "startDateTime": listing.start_date_time,
            "duration": str(listing.duration_ms),
            "venue_name": listing.venue_name,
            "address": listing.address,
            "publishStatus": "DRAFT",
            "graphic": None,
        }

    def _event_data(self, external_id: str) -> dict | None:
        data = self.client.execute("MeetupEventRead", EVENT_READ, {"id": external_id})
        event = data.get("event")
        return event if isinstance(event, dict) else None

    def _matches(
        self,
        remote: dict,
        listing: RenderedMeetupEvent,
        target: dict,
        external_id: str,
        group: dict,
    ) -> bool:
        remote_time = _parse_remote_datetime(remote.get("dateTime"))
        expected_time = datetime.fromisoformat(listing.start_date_time).astimezone(UTC)
        remote_group = remote.get("group") or {}
        return (
            str(remote.get("id")) == external_id
            and remote.get("title") == listing.title
            and remote_group.get("urlname") == target["group_urlname"]
            and group.get("urlname") == target["group_urlname"]
            and remote_time == expected_time
            and _duration_milliseconds(remote.get("duration")) == listing.duration_ms
            and remote.get("eventType") == "PHYSICAL"
            and remote.get("description") == listing.description
            and _remote_address_matches(remote.get("venue") or {}, listing)
        )

    def _safe_url(self, remote: dict, target: dict, external_id: str) -> str | None:
        value = remote.get("eventUrl")
        if not isinstance(value, str) or not MEETUP_EVENT_URL.fullmatch(value):
            return None
        parsed = urlsplit(value)
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            len(path_parts) != 3
            or path_parts[0] != target["group_urlname"]
            or path_parts[2] != external_id
        ):
            return None
        return value

    def _payload_event(self, payload: dict | None, *, stage: str, may_have_applied=False):
        errors = _payload_errors(payload)
        if errors:
            event = payload.get("event") if isinstance(payload, dict) else None
            event_id = str(event.get("id")) if isinstance(event, dict) and event.get("id") else None
            category = _graphql_error_category(errors)
            definitely_rejected = category in {
                "AUTHENTICATION",
                "PERMISSION",
                "RATE_LIMIT",
                "VALIDATION",
            }
            raise MeetupError(
                category,
                code=_graphql_error_code(errors),
                may_have_applied=bool(event_id) or (may_have_applied and not definitely_rejected),
                external_id=event_id,
            )
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict) or not event.get("id"):
            raise MeetupError("UNKNOWN_OUTCOME", may_have_applied=may_have_applied)
        return event

    def execute(self, plan_entry: dict, idempotency_key: str, attempt_number: int, context=None):
        event_id = context.get("event_id") if context else None
        execution_id = context.get("execution_id") if context else None
        if not event_id or not execution_id:
            return self._blocked_result("MEETUP_EXECUTION_CONTEXT_MISSING", "PREFLIGHT")
        try:
            self.validate(plan_entry, "LIVE")
            listing = render(plan_entry)
            target = _target(plan_entry)
        except (HTTPException, MeetupBlocked) as error:
            code = str(error.detail) if isinstance(error, HTTPException) else error.code
            return self._blocked_result(code, "PREFLIGHT")
        account = account_key(target)
        binding = self.repository.binding(event_id, account)
        if (
            binding
            and (binding.get("metadata") or {}).get("group_urlname") != target["group_urlname"]
        ):
            return self._blocked_result("MEETUP_GROUP_CHANGE_UNSUPPORTED", "PREFLIGHT")
        try:
            member = self.client.execute("MeetupSelfReadiness", SELF_READ).get("self")
            if (
                not isinstance(member, dict)
                or str(member.get("id")) != self.settings.meetup_authorized_member_id
            ):
                return self._blocked_result("MEETUP_ACCOUNT_MISMATCH", "READINESS")
            group = _group_and_network(self.client, target)
            venue_id = _venue_id(group, listing)
            rendered = self.render(plan_entry)
            known_id = binding.get("external_id") if binding else None
            operation = "UPDATE" if known_id else "CREATE"
            LOGGER.info("meetup_%s_start attempt=%s", operation.lower(), attempt_number)
            if known_id:
                remote = self._event_data(known_id)
                if remote is None:
                    return self._blocked_result("MEETUP_BOUND_EVENT_NOT_FOUND", "READ")
                if (remote.get("group") or {}).get("urlname") != target["group_urlname"]:
                    return self._blocked_result("MEETUP_GROUP_CHANGE_UNSUPPORTED", "PREFLIGHT")
                status = remote.get("status")
                if status not in {"DRAFT", "ACTIVE"}:
                    return self._blocked_result("MEETUP_EVENT_STATE_UNSUPPORTED", "PREFLIGHT")
                publish_status = "PUBLISHED" if status == "ACTIVE" else "DRAFT"
                input_data = {
                    "eventId": known_id,
                    "title": rendered["title"],
                    "description": rendered["description"],
                    "startDateTime": rendered["startDateTime"],
                    "duration": rendered["duration"],
                    "venueId": venue_id,
                    "publishStatus": publish_status,
                }
                edit_data = self.client.execute(
                    "MeetupEventEdit", EVENT_EDIT, {"input": input_data}, mutation=True
                )
                self._payload_event(
                    edit_data.get("editEvent"), stage="UPDATE", may_have_applied=True
                )
                external_id = known_id
            else:
                input_data = {
                    "groupUrlname": target["group_urlname"],
                    "title": rendered["title"],
                    "description": rendered["description"],
                    "startDateTime": rendered["startDateTime"],
                    "duration": listing.duration_create,
                    "venueId": venue_id,
                    "publishStatus": "DRAFT",
                    "selfRsvp": False,
                }
                created_data = self.client.execute(
                    "MeetupEventCreate", EVENT_CREATE, {"input": input_data}, mutation=True
                )
                try:
                    created = self._payload_event(
                        created_data.get("createEvent"), stage="CREATE", may_have_applied=True
                    )
                except MeetupError as error:
                    if error.external_id:
                        try:
                            self.repository.bind_created(
                                event_id,
                                account,
                                error.external_id,
                                execution_id,
                                target["group_urlname"],
                            )
                        except IntegrityError:
                            pass
                    raise
                external_id = str(created["id"])
                try:
                    self.repository.bind_created(
                        event_id, account, external_id, execution_id, target["group_urlname"]
                    )
                except IntegrityError:
                    raise MeetupError(
                        "UNKNOWN_OUTCOME", may_have_applied=True, external_id=external_id
                    ) from None
                LOGGER.info("meetup_resource_bound")
            draft = self._event_data(external_id)
            if draft is None or not self._matches(draft, listing, target, external_id, group):
                return self._unknown_result(
                    external_id,
                    "DRAFT_VERIFY",
                    "Meetup accepted the draft, but it did not match the approved Event.",
                )
            if draft.get("status") == "DRAFT":
                published_data = self.client.execute(
                    "MeetupEventEdit",
                    EVENT_EDIT,
                    {"input": {"eventId": external_id, "publishStatus": "PUBLISHED"}},
                    mutation=True,
                )
                self._payload_event(
                    published_data.get("editEvent"), stage="PUBLISH", may_have_applied=True
                )
            remote = self._event_data(external_id)
            url = self._safe_url(remote or {}, target, external_id)
            if (
                remote is None
                or not self._matches(remote, listing, target, external_id, group)
                or remote.get("status") != "ACTIVE"
                or not url
            ):
                return self._unknown_result(
                    external_id,
                    "VERIFY",
                    "Meetup accepted the request, but the published Event could not be verified.",
                )
            self.repository.record(
                event_id, account, execution_id, state="PUBLISHED", external_url=url, verified=True
            )
            LOGGER.info("meetup_verified operation=%s", operation.lower())
            return NormalizedResult(
                outcome="PUBLISHED",
                external_id=external_id,
                external_url=url,
                verification_status="VERIFIED",
                evidence={
                    "adapter": ADAPTER_REVISION,
                    "operation": operation,
                    "verification": "VERIFIED_LIVE",
                    "remote_status": remote.get("status"),
                    "group_urlname": target["group_urlname"],
                },
            )
        except MeetupBlocked as blocked:
            return self._blocked_result(blocked.code, "PREFLIGHT")
        except MeetupError as error:
            return _error_result(
                error,
                "CREATE" if not binding else "UPDATE",
                binding.get("external_id") if binding else None,
            )

    def verify(self, plan_entry: dict, idempotency_key: str, context=None) -> NormalizedResult:
        try:
            self.validate(plan_entry, "LIVE")
            target = _target(plan_entry)
            listing = render(plan_entry)
            account = account_key(target)
            binding = self.repository.binding(context["event_id"], account) if context else None
            if not binding:
                return self._inconclusive("NO_EXTERNAL_ID")
            remote = self._event_data(binding["external_id"])
            if remote is None:
                return self._inconclusive("READ_FAILED", binding["external_id"])
            group = _group_and_network(self.client, target)
            url = self._safe_url(remote, target, binding["external_id"])
            if (
                self._matches(remote, listing, target, binding["external_id"], group)
                and remote.get("status") == "ACTIVE"
                and url
            ):
                self.repository.record(
                    context["event_id"],
                    account,
                    context["execution_id"],
                    state="PUBLISHED",
                    external_url=url,
                    verified=True,
                )
                return NormalizedResult(
                    outcome="VERIFICATION_RESULT",
                    external_id=binding["external_id"],
                    external_url=url,
                    verification_status="VERIFIED",
                    verification_finding="FOUND_MATCHING",
                    evidence={"adapter": ADAPTER_REVISION, "verification": "VERIFIED_LIVE"},
                )
            if remote.get("status") == "DRAFT" and self._matches(
                remote, listing, target, binding["external_id"], group
            ):
                return NormalizedResult(
                    outcome="VERIFICATION_RESULT",
                    external_id=binding["external_id"],
                    human_error=(
                        "The approved Meetup draft is ready; retrying will publish that same Event."
                    ),
                    error_category="TRANSIENT_CONFIRMED_NO_EFFECT",
                    retryable=True,
                    verification_status="VERIFIED",
                    verification_finding="CONFIRMED_ABSENT",
                    evidence={"adapter": ADAPTER_REVISION, "remote_status": "DRAFT"},
                )
            return self._inconclusive("MISMATCH", binding["external_id"])
        except (MeetupError, MeetupBlocked, HTTPException):
            return self._inconclusive("READ_FAILED")

    @staticmethod
    def _blocked_result(code: str, stage: str) -> NormalizedResult:
        messages = {
            "MEETUP_GRAPHIC_UPLOAD_UNVERIFIED": (
                "Meetup graphic upload is not yet verified for live publishing."
            ),
            "MEETUP_EVENT_TYPE_UNVERIFIED": (
                "Meetup live publishing currently supports in-person Events only."
            ),
            "MEETUP_VENUE_UNAVAILABLE": (
                "A matching Meetup Group venue could not be verified. Nothing was changed."
            ),
            "MEETUP_GROUP_CHANGE_UNSUPPORTED": (
                "This Meetup Event is already bound to another Group and "
                "cannot be moved automatically."
            ),
            "MEETUP_PAID_EVENT_UNSUPPORTED": (
                "Paid Meetup events need verified payment setup. Nothing was changed to free."
            ),
            "MEETUP_PRICE_UNKNOWN": (
                "Confirm whether this Event is free before live Meetup publishing."
            ),
            "MEETUP_DURATION_REQUIRED": (
                "Meetup needs a known Event end time so its duration can be calculated."
            ),
            "MEETUP_LOCATION_REQUIRED": ("Meetup needs a verified in-person location and address."),
            "MEETUP_EVENT_STATE_UNSUPPORTED": (
                "The existing Meetup Event is in a state this workflow cannot update safely."
            ),
            "MEETUP_BOUND_EVENT_NOT_FOUND": (
                "The linked Meetup Event could not be found. A duplicate was not created."
            ),
            "MEETUP_ACCOUNT_CHANGED": (
                "The approved Meetup account no longer matches the configured account."
            ),
        }
        return NormalizedResult(
            outcome="BLOCKED",
            human_error=messages.get(code, "Meetup cannot publish this approved version yet."),
            error_category=code,
            retryable=False,
            verification_status="VERIFIED",
            evidence={"adapter": ADAPTER_REVISION, "stage": stage, "blocker": code},
        )

    @staticmethod
    def _unknown_result(external_id, stage, message) -> NormalizedResult:
        return NormalizedResult(
            outcome="UNCERTAIN",
            external_id=external_id,
            human_error=message,
            error_category="AMBIGUOUS_OUTCOME",
            verification_status="INCONCLUSIVE",
            evidence={
                "adapter": ADAPTER_REVISION,
                "stage": stage,
                "external_id_known": external_id is not None,
            },
        )

    @staticmethod
    def _inconclusive(reason, external_id=None) -> NormalizedResult:
        return NormalizedResult(
            outcome="VERIFICATION_RESULT",
            external_id=external_id,
            human_error=(
                "Meetup could not confirm the result yet. Do not retry until it is reconciled."
            ),
            error_category="AMBIGUOUS_OUTCOME",
            verification_status="INCONCLUSIVE",
            verification_finding="INCONCLUSIVE",
            evidence={"adapter": ADAPTER_REVISION, "reason": reason},
        )
