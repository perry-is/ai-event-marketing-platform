"""Live Eventbrite connector (API v3, server-side private token).

The adapter mechanically maps one exact approved plan entry to Eventbrite operations:
venue -> draft event -> ticket class -> description -> approved logo -> publish -> verify.
It never rewrites copy, invents facts, or chooses a mode; the approved plan entry decides.
A verified remote event is bound to the local Event so later approved versions UPDATE it.
"""

import html
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx2
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import CampaignVersionAsset, DestinationResourceBinding, GeneratedAsset
from app.simulated_connectors import AdapterDeclaration, NormalizedResult

LOGGER = logging.getLogger("event_marketing.eventbrite")
API_BASE = "https://www.eventbriteapi.com/v3"
ADAPTER_REVISION = "eventbrite-live-v1"
DESTINATION = "EVENTBRITE"
TICKET_NAME = "General Admission"
LIVE_STATUSES = {"live", "started"}
EVENT_URL = re.compile(r"^https://(?:www\.)?eventbrite\.[a-z.]{2,12}/", re.IGNORECASE)
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
}  # fmt: skip
ADDRESS = re.compile(
    r"^(?P<street>[^,]+),\s*(?P<city>[^,]+),\s*(?P<region>[A-Z]{2})"
    r"(?:\s+(?P<postal>\d{5}(?:-\d{4})?))?$"
)
BLOCK_MESSAGES = {
    "EVENTBRITE_TOKEN_MISSING": "Eventbrite isn't connected yet (no access token configured).",
    "EVENTBRITE_ORGANIZATION_MISSING": "Eventbrite organization isn't configured.",
    "EVENTBRITE_ORGANIZER_MISSING": "Eventbrite organizer isn't configured.",
    "EVENTBRITE_CURRENCY_MISSING": "Eventbrite account currency isn't configured.",
    "EVENTBRITE_ACCOUNT_CHANGED": (
        "The approved Eventbrite account no longer matches the connected account."
    ),
    "EVENTBRITE_LISTING_MISSING": "The approved Eventbrite listing details are missing.",
    "EVENTBRITE_NAME_REQUIRED": "Eventbrite needs the Event name.",
    "EVENTBRITE_SCHEDULE_REQUIRED": "Eventbrite needs a start time, end time and time zone.",
    "EVENTBRITE_END_BEFORE_START": "The Event ends before it starts.",
    "EVENTBRITE_ADDRESS_REQUIRED": (
        "Eventbrite needs a street address in the form “123 Main St, City, ST 12345”."
    ),
    "EVENTBRITE_VENUE_NAME_REQUIRED": "Eventbrite needs the venue name.",
    "EVENTBRITE_CAPACITY_REQUIRED": (
        "Eventbrite tickets need an attendee capacity. Add the capacity in Event details."
    ),
    "EVENTBRITE_PAID_TICKETS_UNSUPPORTED": (
        "Paid Eventbrite tickets need an approved price and payout setup, which aren't "
        "available yet. Nothing was changed to free."
    ),
    "EVENTBRITE_PRICE_UNKNOWN": "Eventbrite needs to know whether the Event is free or paid.",
    "EVENTBRITE_DESCRIPTION_REQUIRED": "The approved Eventbrite description is empty.",
    "APPROVED_GRAPHIC_INVALID": "The approved graphic could not be read exactly.",
}


class EventbriteBlocked(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class EventbriteError(Exception):
    category: str
    status_code: int | None = None
    provider_code: str | None = None
    may_have_applied: bool = False

    def __str__(self) -> str:
        return f"{self.category}:{self.status_code}:{self.provider_code}"


# --- Credentials and account -----------------------------------------------------------------


class CredentialProvider(Protocol):
    """Isolated so OAuth can replace private-token lookup without touching the adapter."""

    def authorization_header(self) -> str | None: ...


class PrivateTokenCredentials:
    def __init__(self, settings):
        self._settings = settings

    def authorization_header(self) -> str | None:
        token = self._settings.eventbrite_private_token.get_secret_value().strip()
        return f"Bearer {token}" if token else None


def configured_account(settings) -> dict | None:
    if not (
        settings.eventbrite_organization_id
        and settings.eventbrite_organizer_id
        and settings.eventbrite_currency
    ):
        return None
    return {
        "organization_id": settings.eventbrite_organization_id,
        "organizer_id": settings.eventbrite_organizer_id,
        "currency": settings.eventbrite_currency,
    }


def account_key(account: dict) -> str:
    return f"organization:{account['organization_id']}"


def configuration_blockers(settings) -> list[str]:
    blockers = []
    if not settings.eventbrite_private_token.get_secret_value().strip():
        blockers.append("EVENTBRITE_TOKEN_MISSING")
    if not settings.eventbrite_organization_id:
        blockers.append("EVENTBRITE_ORGANIZATION_MISSING")
    if not settings.eventbrite_organizer_id:
        blockers.append("EVENTBRITE_ORGANIZER_MISSING")
    if not settings.eventbrite_currency:
        blockers.append("EVENTBRITE_CURRENCY_MISSING")
    return blockers


# --- HTTP client -----------------------------------------------------------------------------


def _provider_code(response) -> str | None:
    try:
        value = response.json().get("error")
    except Exception:
        return None
    if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", value):
        return value
    return None


class EventbriteClient:
    """Thin official-API wrapper; normalizes failures and never logs credentials."""

    def __init__(self, credentials: CredentialProvider, http_client=None, timeout: float = 20.0):
        self.credentials = credentials
        self._owns_http = http_client is None
        self.http = http_client or httpx2.Client(timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def request(self, method: str, path: str, *, json_body=None, params=None) -> dict:
        authorization = self.credentials.authorization_header()
        if not authorization:
            raise EventbriteError("CONFIGURATION")
        mutating = method.upper() != "GET"
        try:
            response = self.http.request(
                method,
                f"{API_BASE}{path}",
                json=json_body,
                params=params,
                headers={"Authorization": authorization, "Accept": "application/json"},
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            raise EventbriteError("NETWORK", may_have_applied=False) from None
        except httpx2.HTTPError:
            # Read/write timeouts and protocol errors may follow remote acceptance.
            raise EventbriteError("NETWORK", may_have_applied=mutating) from None
        status = response.status_code
        if status in {200, 201}:
            try:
                data = response.json()
            except ValueError:
                raise EventbriteError(
                    "UNKNOWN_OUTCOME", status, may_have_applied=mutating
                ) from None
            if not isinstance(data, dict):
                raise EventbriteError("UNKNOWN_OUTCOME", status, may_have_applied=mutating)
            return data
        code = _provider_code(response)
        if status == 401:
            raise EventbriteError("AUTHENTICATION", status, code)
        if status == 403:
            raise EventbriteError("PERMISSION", status, code)
        if status == 404:
            raise EventbriteError("NOT_FOUND", status, code)
        if status == 429:
            raise EventbriteError("RATE_LIMIT", status, code)
        if 400 <= status < 500:
            raise EventbriteError("VALIDATION", status, code)
        raise EventbriteError("REMOTE_SERVER", status, code, may_have_applied=mutating)

    def upload_file(self, url: str, fields: dict, file_field: str, data: bytes, mime: str):
        """Upload to the provider-issued URL without sending the Eventbrite credential."""
        if not url.startswith("https://"):
            raise EventbriteError("VALIDATION")
        try:
            response = self.http.post(
                url,
                data={key: str(value) for key, value in fields.items()},
                files={file_field: ("campaign-graphic", data, mime)},
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            raise EventbriteError("NETWORK", may_have_applied=False) from None
        except httpx2.HTTPError:
            raise EventbriteError("NETWORK", may_have_applied=True) from None
        if response.status_code >= 400:
            raise EventbriteError(
                "RATE_LIMIT" if response.status_code == 429 else "REMOTE_SERVER",
                response.status_code,
            )


# --- Readiness -------------------------------------------------------------------------------


def _paged(client: EventbriteClient, path: str, key: str, limit_pages: int = 5):
    params: dict = {}
    for _ in range(limit_pages):
        data = client.request("GET", path, params=params or None)
        yield from data.get(key) or []
        pagination = data.get("pagination") or {}
        if not pagination.get("has_more_items") or not pagination.get("continuation"):
            return
        params = {"continuation": pagination["continuation"]}


def live_readiness(settings, client: EventbriteClient | None) -> dict:
    """Read-only probe: token, current user, organization and organizer. Never writes."""
    blockers = configuration_blockers(settings)
    result = {
        "mode": settings.eventbrite_execution_mode,
        "configured": not blockers,
        "status": "NEEDS_SETUP" if blockers else "READY",
        "reasons": blockers,
        "organization_name": None,
        "organizer_name": None,
        "checked_live": False,
    }
    if blockers or client is None:
        return result
    try:
        client.request("GET", "/users/me/")
        organization = next(
            (
                item
                for item in _paged(client, "/users/me/organizations/", "organizations")
                if str(item.get("id")) == settings.eventbrite_organization_id
            ),
            None,
        )
        if organization is None:
            result.update(status="NEEDS_SETUP", reasons=["EVENTBRITE_ORGANIZATION_UNAVAILABLE"])
            return result
        result["organization_name"] = str(organization.get("name") or "")[:120] or None
        organizer = next(
            (
                item
                for item in _paged(
                    client,
                    f"/organizations/{settings.eventbrite_organization_id}/organizers/",
                    "organizers",
                )
                if str(item.get("id")) == settings.eventbrite_organizer_id
            ),
            None,
        )
        if organizer is None:
            result.update(status="NEEDS_SETUP", reasons=["EVENTBRITE_ORGANIZER_UNAVAILABLE"])
            return result
        result["organizer_name"] = str(organizer.get("name") or "")[:120] or None
        result["checked_live"] = True
    except EventbriteError as error:
        reason = {
            "AUTHENTICATION": "EVENTBRITE_TOKEN_REJECTED",
            "PERMISSION": "EVENTBRITE_PERMISSION_DENIED",
            "RATE_LIMIT": "EVENTBRITE_RATE_LIMITED",
        }.get(error.category, "EVENTBRITE_UNREACHABLE")
        result.update(status="UNAVAILABLE", reasons=[reason])
    LOGGER.info("eventbrite_readiness status=%s reasons=%s", result["status"], result["reasons"])
    return result


# --- Deterministic rendering -----------------------------------------------------------------


def listing_facts(event_snapshot: dict) -> dict:
    """Snapshot facts frozen into the approved plan entry (no defaults are invented)."""
    facts = event_snapshot.get("facts") or {}
    location = facts.get("location") or {}
    registration = facts.get("registration") or {}
    return {
        "name": event_snapshot.get("title"),
        "starts_at": event_snapshot.get("starts_at"),
        "ends_at": event_snapshot.get("ends_at"),
        "timezone": event_snapshot.get("timezone"),
        "delivery": event_snapshot.get("delivery"),
        "venue_name": location.get("venue"),
        "address": location.get("address"),
        "price_kind": registration.get("price_kind"),
        "amount_minor": registration.get("amount_minor"),
        "currency": registration.get("currency"),
        "capacity": registration.get("capacity"),
    }


@dataclass
class RenderedListing:
    name: str
    start: dict
    end: dict
    online: bool
    venue: dict | None
    ticket: dict
    description_html: str
    currency: str
    organizer_id: str
    graphic: dict | None
    plain_name: str = field(default="")


def _utc(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def description_html(text: str) -> str:
    """Exact approved text, HTML-escaped; blank lines become paragraphs, newlines <br>."""
    paragraphs = [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    return "".join(
        "<p>" + "<br>".join(html.escape(line) for line in part.split("\n")) + "</p>"
        for part in paragraphs
    )


def parse_address(address: str) -> dict | None:
    match = ADDRESS.match(" ".join(address.split()))
    if not match or match.group("region") not in US_STATE_CODES:
        return None
    return {
        "address_1": match.group("street").strip(),
        "city": match.group("city").strip(),
        "region": match.group("region"),
        "postal_code": match.group("postal"),
        "country": "US",
    }


def render(entry: dict, account: dict | None) -> RenderedListing:
    eventbrite = entry.get("eventbrite") or {}
    approved_account = eventbrite.get("account")
    listing = eventbrite.get("listing")
    if not approved_account or not listing:
        raise EventbriteBlocked("EVENTBRITE_LISTING_MISSING")
    if account is None or approved_account != account:
        raise EventbriteBlocked("EVENTBRITE_ACCOUNT_CHANGED")
    name = (listing.get("name") or "").strip()
    if not name:
        raise EventbriteBlocked("EVENTBRITE_NAME_REQUIRED")
    if not (listing.get("starts_at") and listing.get("ends_at") and listing.get("timezone")):
        raise EventbriteBlocked("EVENTBRITE_SCHEDULE_REQUIRED")
    try:
        ZoneInfo(listing["timezone"])
        start_utc, end_utc = _utc(listing["starts_at"]), _utc(listing["ends_at"])
    except (ValueError, KeyError):
        raise EventbriteBlocked("EVENTBRITE_SCHEDULE_REQUIRED") from None
    if end_utc <= start_utc:
        raise EventbriteBlocked("EVENTBRITE_END_BEFORE_START")
    delivery = listing.get("delivery")
    venue = None
    if delivery in {"IN_PERSON", "HYBRID"}:
        if not (listing.get("venue_name") or "").strip():
            raise EventbriteBlocked("EVENTBRITE_VENUE_NAME_REQUIRED")
        address = parse_address(listing.get("address") or "")
        if address is None:
            raise EventbriteBlocked("EVENTBRITE_ADDRESS_REQUIRED")
        venue = {"name": " ".join(listing["venue_name"].split()), "address": address}
    elif delivery != "VIRTUAL":
        raise EventbriteBlocked("EVENTBRITE_SCHEDULE_REQUIRED")
    price_kind = listing.get("price_kind")
    if price_kind == "PAID":
        raise EventbriteBlocked("EVENTBRITE_PAID_TICKETS_UNSUPPORTED")
    if price_kind != "FREE":
        raise EventbriteBlocked("EVENTBRITE_PRICE_UNKNOWN")
    capacity = listing.get("capacity")
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
        raise EventbriteBlocked("EVENTBRITE_CAPACITY_REQUIRED")
    text = entry.get("approved_content")
    if not isinstance(text, str) or not text.strip():
        raise EventbriteBlocked("EVENTBRITE_DESCRIPTION_REQUIRED")
    return RenderedListing(
        name=html.escape(name),
        plain_name=name,
        start={"timezone": listing["timezone"], "utc": start_utc},
        end={"timezone": listing["timezone"], "utc": end_utc},
        online=delivery == "VIRTUAL",
        venue=venue,
        ticket={"name": TICKET_NAME, "free": True, "quantity_total": capacity},
        description_html=description_html(text),
        currency=account["currency"],
        organizer_id=account["organizer_id"],
        graphic=entry.get("approved_graphic"),
    )


# --- Persistence boundary (bindings and approved asset bytes) -------------------------------


class EventbriteRepository:
    def __init__(self, sessions: sessionmaker, settings):
        self.sessions = sessions
        self.settings = settings

    def binding(self, event_id, account: dict) -> dict | None:
        with self.sessions() as db:
            row = db.scalar(
                select(DestinationResourceBinding).where(
                    DestinationResourceBinding.destination_key == DESTINATION,
                    DestinationResourceBinding.account_key == account_key(account),
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

    def bind_created(self, event_id, account: dict, external_id: str, execution_id) -> None:
        """Persist the new remote identity immediately, before any further remote step."""
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            db.add(
                DestinationResourceBinding(
                    id=uuid.uuid4(),
                    destination_key=DESTINATION,
                    account_key=account_key(account),
                    event_id=uuid.UUID(str(event_id)),
                    external_id=external_id,
                    external_url=None,
                    state="DRAFT",
                    resource_metadata={},
                    bound_by_execution_id=uuid.UUID(str(execution_id)),
                    last_execution_id=uuid.UUID(str(execution_id)),
                    revision=1,
                    first_bound_at=now,
                    last_verified_at=None,
                    updated_at=now,
                )
            )

    def record(
        self,
        event_id,
        account: dict,
        execution_id,
        *,
        metadata: dict | None = None,
        state: str | None = None,
        external_url: str | None = None,
        verified: bool = False,
    ) -> None:
        with self.sessions.begin() as db:
            row = db.scalar(
                select(DestinationResourceBinding)
                .where(
                    DestinationResourceBinding.destination_key == DESTINATION,
                    DestinationResourceBinding.account_key == account_key(account),
                    DestinationResourceBinding.event_id == uuid.UUID(str(event_id)),
                )
                .with_for_update()
            )
            if row is None:
                return
            now = datetime.now(UTC)
            if metadata:
                row.resource_metadata = {**(row.resource_metadata or {}), **metadata}
            if state:
                row.state = state
            if external_url:
                row.external_url = external_url
            if verified:
                row.last_verified_at = now
            row.last_execution_id = uuid.UUID(str(execution_id))
            row.revision += 1
            row.updated_at = now

    def approved_graphic_bytes(self, campaign_version_id: str, graphic: dict) -> tuple[bytes, str]:
        from app.graphics_service import _read_asset

        with self.sessions() as db:
            row = db.execute(
                select(CampaignVersionAsset, GeneratedAsset)
                .join(GeneratedAsset, GeneratedAsset.id == CampaignVersionAsset.asset_id)
                .where(
                    CampaignVersionAsset.version_id == uuid.UUID(campaign_version_id),
                    CampaignVersionAsset.role == "PRIMARY_GRAPHIC",
                )
            ).one_or_none()
            if row is None:
                raise EventbriteBlocked("APPROVED_GRAPHIC_INVALID")
            _manifest, asset = row
            if str(asset.id) != graphic.get("asset_id") or asset.sha256 != graphic.get("sha256"):
                raise EventbriteBlocked("APPROVED_GRAPHIC_INVALID")
            try:
                return _read_asset(self.settings, asset), asset.mime_type
            except HTTPException:
                raise EventbriteBlocked("APPROVED_GRAPHIC_INVALID") from None


# --- Live adapter ----------------------------------------------------------------------------


def _venue_fingerprint(venue: dict) -> str:
    address = venue["address"]
    parts = [venue["name"], *(address.get(k) or "" for k in ("address_1", "city", "region"))]
    parts.append(address.get("postal_code") or "")
    return "|".join(" ".join(part.casefold().split()) for part in parts)


def _remote_venue_fingerprint(item: dict) -> str | None:
    address = item.get("address") or {}
    if not item.get("name") or not address.get("address_1"):
        return None
    return _venue_fingerprint(
        {
            "name": item["name"],
            "address": {
                "address_1": address.get("address_1"),
                "city": address.get("city"),
                "region": address.get("region"),
                "postal_code": address.get("postal_code"),
            },
        }
    )


class _StepFailure(Exception):
    def __init__(self, error: EventbriteError, stage: str):
        super().__init__(stage)
        self.error = error
        self.stage = stage


class EventbriteLiveAdapter:
    accepts_context = True

    def __init__(self, settings, repository: EventbriteRepository, client: EventbriteClient):
        self.settings = settings
        self.repository = repository
        self.client = client
        ready = not configuration_blockers(settings)
        self.declaration = AdapterDeclaration(
            destination_key=DESTINATION,
            execution_kind="AUTO",
            readiness_simulated="BLOCKED",
            readiness_live="READY" if ready else "BLOCKED",
            adapter_revision=ADAPTER_REVISION,
            can_verify=True,
        )

    def close(self) -> None:
        self.client.close()

    @property
    def account(self) -> dict | None:
        return configured_account(self.settings)

    def blocker(self) -> str | None:
        blockers = configuration_blockers(self.settings)
        return blockers[0] if blockers else None

    def validate(self, plan_entry: dict, runtime_mode: str) -> None:
        if runtime_mode != "LIVE":
            raise HTTPException(409, "APPROVED_CONNECTOR_MODE_UNAVAILABLE")
        if (
            plan_entry.get("destination_key") != DESTINATION
            or plan_entry.get("execution_kind") != "AUTO"
        ):
            raise HTTPException(409, "ADAPTER_DESTINATION_MISMATCH")
        blocker = self.blocker()
        if blocker:
            raise HTTPException(409, blocker)
        try:
            render(plan_entry, self.account)
        except EventbriteBlocked as blocked:
            raise HTTPException(409, blocked.code) from None

    def render(self, plan_entry: dict) -> dict:
        listing = render(plan_entry, self.account)
        return {
            "name": listing.plain_name,
            "start": listing.start,
            "end": listing.end,
            "online": listing.online,
            "venue": listing.venue,
            "ticket": listing.ticket,
            "description_html": listing.description_html,
        }

    # -- remote steps -------------------------------------------------------------------------

    def _step(self, stage: str, method: str, path: str, **kwargs) -> dict:
        try:
            return self.client.request(method, path, **kwargs)
        except EventbriteError as error:
            raise _StepFailure(error, stage) from None

    def _venue(self, listing: RenderedListing, binding: dict | None, org: str) -> str | None:
        if listing.venue is None:
            return None
        fingerprint = _venue_fingerprint(listing.venue)
        known = (binding or {}).get("metadata", {})
        if known.get("venue_fingerprint") == fingerprint and known.get("venue_id"):
            return known["venue_id"]
        try:
            for item in _paged(self.client, f"/organizations/{org}/venues/", "venues", 3):
                if _remote_venue_fingerprint(item) == fingerprint and item.get("id"):
                    LOGGER.info("eventbrite_venue_reused")
                    return str(item["id"])
        except EventbriteError as error:
            raise _StepFailure(error, "VENUE_LOOKUP") from None
        created = self._step(
            "VENUE_CREATE",
            "POST",
            f"/organizations/{org}/venues/",
            json_body={"venue": listing.venue},
        )
        if not created.get("id"):
            raise _StepFailure(EventbriteError("UNKNOWN_OUTCOME", may_have_applied=True), "VENUE")
        LOGGER.info("eventbrite_venue_created")
        return str(created["id"])

    def _event_body(self, listing: RenderedListing, venue_id: str | None) -> dict:
        body = {
            "name": {"html": listing.name},
            "start": listing.start,
            "end": listing.end,
            "currency": listing.currency,
            "online_event": listing.online,
            "organizer_id": listing.organizer_id,
            "listed": True,
        }
        if venue_id:
            body["venue_id"] = venue_id
        return {"event": body}

    def _ticket(self, event_id: str, listing: RenderedListing, known: dict) -> str:
        ticket_id = known.get("ticket_class_id")
        body = {"ticket_class": listing.ticket}
        if ticket_id:
            self._step(
                "TICKET_UPDATE", "POST", f"/events/{event_id}/ticket_classes/{ticket_id}/",
                json_body=body,
            )  # fmt: skip
            return ticket_id
        created = self._step(
            "TICKET_CREATE", "POST", f"/events/{event_id}/ticket_classes/", json_body=body
        )
        if not created.get("id"):
            raise _StepFailure(EventbriteError("UNKNOWN_OUTCOME", may_have_applied=True), "TICKET")
        LOGGER.info("eventbrite_ticket_created")
        return str(created["id"])

    def _description(self, event_id: str, listing: RenderedListing) -> None:
        current = self._step(
            "DESCRIPTION_READ", "GET", f"/events/{event_id}/structured_content/edit/"
        )
        version = int(current.get("page_version_number") or 0) + 1
        self._step(
            "DESCRIPTION_SET",
            "POST",
            f"/events/{event_id}/structured_content/{version}/",
            json_body={
                "modules": [
                    {
                        "type": "text",
                        "data": {
                            "body": {
                                "type": "text",
                                "text": listing.description_html,
                                "alignment": "left",
                            }
                        },
                    }
                ],
                "publish": True,
                "purpose": "listing",
            },
        )

    def _logo(self, event_id: str, listing: RenderedListing, plan_entry: dict, known: dict):
        graphic = listing.graphic
        if not graphic:
            return None
        if known.get("logo_sha256") == graphic["sha256"] and known.get("logo_id"):
            return known["logo_id"]
        data, mime = self.repository.approved_graphic_bytes(
            plan_entry["campaign_version_id"], graphic
        )
        instructions = self._step(
            "IMAGE_PREPARE", "GET", "/media/upload/", params={"type": "image-event-logo"}
        )
        token = instructions.get("upload_token")
        url = instructions.get("upload_url")
        if not token or not url:
            raise _StepFailure(EventbriteError("VALIDATION"), "IMAGE_PREPARE")
        try:
            self.client.upload_file(
                url,
                instructions.get("upload_data") or {},
                instructions.get("file_parameter_name") or "file",
                data,
                mime,
            )
        except EventbriteError as error:
            raise _StepFailure(error, "IMAGE_UPLOAD") from None
        media = self._step(
            "IMAGE_REGISTER",
            "POST",
            "/media/upload/",
            json_body={
                "upload_token": token,
                "crop_mask": {
                    "top_left": {"x": 0, "y": 0},
                    "width": graphic["width"],
                    "height": graphic["height"],
                },
            },
        )
        if not media.get("id"):
            raise _StepFailure(EventbriteError("UNKNOWN_OUTCOME"), "IMAGE_REGISTER")
        self._step(
            "IMAGE_ATTACH",
            "POST",
            f"/events/{event_id}/",
            json_body={"event": {"logo_id": str(media["id"])}},
        )
        LOGGER.info("eventbrite_image_attached")
        return str(media["id"])

    def _matches(self, remote: dict, listing: RenderedListing, external_id: str) -> bool:
        return (
            str(remote.get("id")) == external_id
            and (remote.get("name") or {}).get("text") == listing.plain_name
            and (remote.get("start") or {}).get("utc") == listing.start["utc"]
            and (remote.get("end") or {}).get("utc") == listing.end["utc"]
        )

    # -- execute ------------------------------------------------------------------------------

    def execute(self, plan_entry: dict, idempotency_key: str, attempt_number: int, context=None):
        if not context or not context.get("event_id") or not context.get("execution_id"):
            return self._result_not_created("CONFIGURATION", "CONTEXT", retryable=False)
        account = self.account
        try:
            self.validate(plan_entry, "LIVE")
            listing = render(plan_entry, account)
        except HTTPException as error:
            return self._result_not_created(str(error.detail), "PREFLIGHT", retryable=False)
        org = account["organization_id"]
        event_id, execution_id = context["event_id"], context["execution_id"]
        binding = self.repository.binding(event_id, account)
        operation = "UPDATE" if binding else "CREATE"
        external_id = binding["external_id"] if binding else None
        known = dict((binding or {}).get("metadata") or {})
        LOGGER.info("eventbrite_%s_start attempt=%s", operation.lower(), attempt_number)
        try:
            venue_id = self._venue(listing, binding, org)
            if venue_id:
                known.update(venue_id=venue_id, venue_fingerprint=_venue_fingerprint(listing.venue))
            if external_id is None:
                created = self._step(
                    "CREATE",
                    "POST",
                    f"/organizations/{org}/events/",
                    json_body=self._event_body(listing, venue_id),
                )
                if not created.get("id"):
                    raise _StepFailure(
                        EventbriteError("UNKNOWN_OUTCOME", may_have_applied=True), "CREATE"
                    )
                external_id = str(created["id"])
                try:
                    self.repository.bind_created(event_id, account, external_id, execution_id)
                except IntegrityError:
                    raise _StepFailure(
                        EventbriteError("UNKNOWN_OUTCOME", may_have_applied=True), "BIND"
                    ) from None
                LOGGER.info("eventbrite_resource_bound")
            else:
                self._step(
                    "UPDATE",
                    "POST",
                    f"/events/{external_id}/",
                    json_body=self._event_body(listing, venue_id),
                )
            self.repository.record(event_id, account, execution_id, metadata=known)
            known["ticket_class_id"] = self._ticket(external_id, listing, known)
            self.repository.record(event_id, account, execution_id, metadata=known)
            self._description(external_id, listing)
            logo_id = self._logo(external_id, listing, plan_entry, known)
            if logo_id:
                known.update(logo_id=logo_id, logo_sha256=listing.graphic["sha256"])
            self.repository.record(event_id, account, execution_id, metadata=known)
            remote = self._step("PRE_PUBLISH_READ", "GET", f"/events/{external_id}/")
            if remote.get("status") not in LIVE_STATUSES:
                published = self._step("PUBLISH", "POST", f"/events/{external_id}/publish/")
                if published.get("published") is not True:
                    raise _StepFailure(EventbriteError("VALIDATION"), "PUBLISH")
                LOGGER.info("eventbrite_publish_accepted")
            remote = self._step("VERIFY", "GET", f"/events/{external_id}/")
        except _StepFailure as failure:
            return self._failure(failure, operation, external_id)
        except EventbriteBlocked as blocked:
            return self._result_not_created(blocked.code, "PREFLIGHT", retryable=False)
        url = str(remote.get("url") or "")
        if (
            not self._matches(remote, listing, external_id)
            or remote.get("status") not in LIVE_STATUSES
            or not EVENT_URL.match(url)
        ):
            LOGGER.warning("eventbrite_verification_mismatch")
            return NormalizedResult(
                outcome="UNCERTAIN",
                external_id=external_id,
                human_error="Eventbrite accepted the request, but verification didn't match.",
                error_category="AMBIGUOUS_OUTCOME",
                verification_status="INCONCLUSIVE",
                evidence={
                    "adapter": ADAPTER_REVISION,
                    "operation": operation,
                    "external_id_known": True,
                    "stage": "VERIFY",
                },
            )
        self.repository.record(
            event_id, account, execution_id, state="PUBLISHED", external_url=url, verified=True
        )
        LOGGER.info("eventbrite_verified operation=%s", operation)
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
                "approved_graphic_sha256": (listing.graphic or {}).get("sha256"),
                "venue_reused": bool(binding and listing.venue),
            },
        )

    def _result_not_created(self, code: str, stage: str, *, retryable: bool) -> NormalizedResult:
        message = BLOCK_MESSAGES.get(code, "Eventbrite can't be updated with these details.")
        return NormalizedResult(
            outcome="NOT_CREATED",
            human_error=message,
            error_category="VALIDATION" if stage == "PREFLIGHT" else code,
            retryable=retryable,
            verification_status="VERIFIED",
            evidence={"adapter": ADAPTER_REVISION, "stage": stage, "blocker": code},
        )

    def _failure(self, failure: _StepFailure, operation: str, external_id: str | None):
        error = failure.error
        LOGGER.warning(
            "eventbrite_step_failed stage=%s category=%s status=%s code=%s",
            failure.stage,
            error.category,
            error.status_code,
            error.provider_code,
        )
        evidence = {
            "adapter": ADAPTER_REVISION,
            "operation": operation,
            "stage": failure.stage,
            "category": error.category,
            "provider_status": error.status_code,
            "provider_code": error.provider_code,
            "external_id_known": external_id is not None,
        }
        if error.may_have_applied:
            message = (
                "Eventbrite may have created the event, but the result could not be verified."
                if failure.stage == "CREATE"
                else "Eventbrite may have applied the change, but it could not be verified."
            )
            return NormalizedResult(
                outcome="UNCERTAIN",
                external_id=external_id,
                human_error=message,
                error_category="AMBIGUOUS_OUTCOME",
                verification_status="INCONCLUSIVE",
                evidence=evidence,
            )
        if external_id:
            evidence["result_summary"] = (
                "Eventbrite didn't finish. The Eventbrite listing is linked, so a retry "
                "updates that same listing."
            )
        messages = {
            "AUTHENTICATION": "Eventbrite rejected the connection. Check the Eventbrite setup.",
            "PERMISSION": "Eventbrite didn't allow this change for the connected account.",
            "RATE_LIMIT": "Eventbrite asked us to slow down. Try again in a few minutes.",
            "VALIDATION": "Eventbrite rejected these details. Nothing was published.",
            "NOT_FOUND": "The linked Eventbrite listing couldn't be found.",
            "NETWORK": "Eventbrite couldn't be reached. Nothing was sent.",
            "REMOTE_SERVER": "Eventbrite had a problem. Nothing was published.",
            "CONFIGURATION": "Eventbrite isn't configured on the server.",
        }
        return NormalizedResult(
            outcome="NOT_CREATED",
            external_id=None,
            human_error=messages.get(error.category, "Eventbrite didn't finish."),
            error_category=error.category,
            # With a bound listing, a retry can only UPDATE that listing, never create another.
            retryable=error.category
            in {"RATE_LIMIT", "NETWORK", "REMOTE_SERVER", "AUTHENTICATION", "PERMISSION"}
            or (external_id is not None and error.category == "VALIDATION"),
            verification_status="VERIFIED",
            evidence=evidence,
        )

    # -- verify (reconciliation; never writes) ------------------------------------------------

    def verify(self, plan_entry: dict, idempotency_key: str, context=None) -> NormalizedResult:
        account = self.account
        try:
            listing = render(plan_entry, account)
        except EventbriteBlocked:
            return self._inconclusive("RENDER")
        binding = self.repository.binding(context["event_id"], account) if context else None
        if not binding:
            # No trustworthy remote identity: never search by title or guess.
            return self._inconclusive("NO_EXTERNAL_ID")
        external_id = binding["external_id"]
        try:
            remote = self.client.request("GET", f"/events/{external_id}/")
        except EventbriteError:
            return self._inconclusive("READ_FAILED", external_id)
        url = str(remote.get("url") or "")
        if (
            self._matches(remote, listing, external_id)
            and remote.get("status") in LIVE_STATUSES
            and EVENT_URL.match(url)
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
                external_id=external_id,
                external_url=url,
                verification_status="VERIFIED",
                verification_finding="FOUND_MATCHING",
                evidence={"adapter": ADAPTER_REVISION, "verification": "VERIFIED_LIVE"},
            )
        if str(remote.get("id")) == external_id:
            # The linked listing exists but doesn't show the approved published state; a retry
            # updates this same listing by ID and can never create another one.
            return NormalizedResult(
                outcome="VERIFICATION_RESULT",
                external_id=external_id,
                human_error=(
                    "The linked Eventbrite listing doesn't show this approved version yet. "
                    "Retrying updates that same listing."
                ),
                error_category="TRANSIENT_CONFIRMED_NO_EFFECT",
                retryable=True,
                verification_status="VERIFIED",
                verification_finding="CONFIRMED_ABSENT",
                evidence={"adapter": ADAPTER_REVISION, "remote_status": remote.get("status")},
            )
        return self._inconclusive("ID_MISMATCH", external_id)

    @staticmethod
    def _inconclusive(reason: str, external_id: str | None = None) -> NormalizedResult:
        return NormalizedResult(
            outcome="VERIFICATION_RESULT",
            external_id=external_id,
            human_error=(
                "Eventbrite may have created the event, but it couldn't be verified safely."
                if reason == "NO_EXTERNAL_ID"
                else "Eventbrite couldn't confirm the result yet."
            ),
            error_category="AMBIGUOUS_OUTCOME",
            verification_status="INCONCLUSIVE",
            verification_finding="INCONCLUSIVE",
            evidence={"adapter": ADAPTER_REVISION, "reason": reason},
        )
