"""Stateful in-memory Eventbrite API v3 double behind an httpx2 MockTransport (tests only).

It exercises the real live adapter's HTTP path; nothing here contacts Eventbrite.
"""

import html
import json
import threading
from urllib.parse import parse_qs

import httpx2

TOKEN = "test-eventbrite-token"
ORGANIZATION_ID = "1000001"
ORGANIZER_ID = "2000002"
UPLOAD_URL = "https://uploads.fake-eventbrite-storage.test/upload"


class FakeEventbrite:
    def __init__(self):
        self.lock = threading.Lock()
        # Remote IDs never repeat across resets, like a real platform.
        self.next_id = 7000000
        self.reset()

    def reset(self):
        self.events: dict[str, dict] = {}
        self.venues: dict[str, dict] = {}
        self.tickets: dict[str, dict] = {}
        self.content: dict[str, dict] = {}
        self.uploads: dict[str, bytes] = {}
        self.media: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.upload_headers: list[dict] = []
        # Scenario switches.
        self.timeout_after_create = False
        self.reject_publish = False
        self.fail_token = False
        self.rate_limit_paths: set[str] = set()
        self.organizations = [{"id": ORGANIZATION_ID, "name": "Event Marketing"}]
        self.organizers = [{"id": ORGANIZER_ID, "name": "Event Marketing Events"}]

    # -- helpers ----------------------------------------------------------------------------

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def creates(self) -> int:
        return sum(
            1 for method, path in self.calls if method == "POST" and path.endswith("/events/")
            and path.startswith("/organizations/")
        )  # fmt: skip

    def _event_view(self, event_id: str) -> dict:
        event = self.events[event_id]
        return {
            "id": event_id,
            "name": {"text": event["name_text"], "html": event["name_html"]},
            "start": event["start"],
            "end": event["end"],
            "status": event["status"],
            "url": f"https://www.eventbrite.com/e/event-marketing-{event_id}",
            "online_event": event.get("online_event"),
            "venue_id": event.get("venue_id"),
            "organizer_id": event.get("organizer_id"),
            "currency": event.get("currency"),
            "logo_id": event.get("logo_id"),
        }

    @staticmethod
    def _json(status: int, body: dict) -> httpx2.Response:
        return httpx2.Response(status, json=body)

    def _apply_event(self, event: dict, body: dict) -> None:
        data = body.get("event") or {}
        if "name" in data:
            event["name_html"] = data["name"]["html"]
            event["name_text"] = html.unescape(data["name"]["html"])
        for key in ("start", "end", "currency", "online_event", "venue_id", "organizer_id",
                    "listed", "logo_id"):  # fmt: skip
            if key in data:
                event[key] = data[key]

    # -- transport --------------------------------------------------------------------------

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        with self.lock:
            return self._handle(request)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        if str(url).startswith(UPLOAD_URL):
            self.upload_headers.append(dict(request.headers))
            body = request.read()
            boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()
            file_bytes = b""
            for part in body.split(b"--" + boundary):
                if b'name="file"' in part:
                    file_bytes = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
            self.uploads["upload-token-1"] = file_bytes
            return httpx2.Response(204)
        assert url.host == "www.eventbriteapi.com", url
        path = url.path.removeprefix("/v3")
        method = request.method
        self.calls.append((method, path))
        if request.headers.get("authorization") != f"Bearer {TOKEN}" or self.fail_token:
            return self._json(401, {"error": "INVALID_AUTH", "status_code": 401})
        if path in self.rate_limit_paths:
            return self._json(429, {"error": "HIT_RATE_LIMIT", "status_code": 429})
        body = json.loads(request.content) if request.content else {}
        parts = [part for part in path.split("/") if part]

        if path == "/users/me/":
            return self._json(200, {"id": "42", "name": "Event Marketing Operator"})
        if path == "/users/me/organizations/":
            return self._json(200, {"organizations": self.organizations, "pagination": {}})
        if parts[:1] == ["organizations"] and parts[2:] == ["organizers"]:
            return self._json(200, {"organizers": self.organizers, "pagination": {}})
        if parts[:1] == ["organizations"] and parts[2:] == ["venues"]:
            if parts[1] != ORGANIZATION_ID:
                return self._json(403, {"error": "NOT_AUTHORIZED"})
            if method == "GET":
                return self._json(200, {"venues": list(self.venues.values()), "pagination": {}})
            venue_id = self._id()
            self.venues[venue_id] = {"id": venue_id, **body["venue"]}
            return self._json(200, self.venues[venue_id])
        if parts[:1] == ["organizations"] and parts[2:] == ["events"]:
            event_id = self._id()
            event = {"status": "draft"}
            self._apply_event(event, body)
            self.events[event_id] = event
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise httpx2.ReadTimeout("simulated timeout after acceptance", request=request)
            return self._json(200, self._event_view(event_id))
        if path == "/media/upload/":
            if method == "GET":
                assert parse_qs(url.query.decode())["type"] == ["image-event-logo"]
                return self._json(
                    200,
                    {
                        "upload_method": "POST",
                        "upload_url": UPLOAD_URL,
                        "upload_data": {"key": "uploads/campaign-graphic"},
                        "file_parameter_name": "file",
                        "upload_token": "upload-token-1",
                    },
                )
            token = body.get("upload_token")
            if token not in self.uploads:
                return self._json(400, {"error": "INVALID_UPLOAD"})
            media_id = self._id()
            self.media[media_id] = {"bytes": self.uploads[token], "crop": body.get("crop_mask")}
            return self._json(200, {"id": media_id})
        if parts[:1] == ["events"] and len(parts) >= 2:
            event_id = parts[1]
            if event_id not in self.events:
                return self._json(404, {"error": "NOT_FOUND"})
            event = self.events[event_id]
            rest = parts[2:]
            if not rest:
                if method == "GET":
                    return self._json(200, self._event_view(event_id))
                self._apply_event(event, body)
                return self._json(200, self._event_view(event_id))
            if rest == ["ticket_classes"]:
                ticket_id = self._id()
                self.tickets[ticket_id] = {"event_id": event_id, **body["ticket_class"]}
                return self._json(200, {"id": ticket_id, **self.tickets[ticket_id]})
            if rest[0] == "ticket_classes" and len(rest) == 2:
                self.tickets[rest[1]].update(body["ticket_class"])
                return self._json(200, {"id": rest[1], **self.tickets[rest[1]]})
            if rest == ["structured_content", "edit"]:
                version = self.content.get(event_id, {}).get("version", 0)
                return self._json(200, {"page_version_number": str(version) if version else None})
            if rest[0] == "structured_content":
                expected = self.content.get(event_id, {}).get("version", 0) + 1
                if int(rest[1]) != expected:
                    return self._json(400, {"error": "VERSION_CONFLICT"})
                self.content[event_id] = {"version": expected, "modules": body["modules"]}
                return self._json(200, {"modules": body["modules"]})
            if rest == ["publish"]:
                ready = (
                    self.reject_publish is False
                    and event.get("organizer_id")
                    and any(t["event_id"] == event_id for t in self.tickets.values())
                    and self.content.get(event_id)
                )
                if not ready:
                    return self._json(400, {"error": "VALIDATION_ERROR", "status_code": 400})
                event["status"] = "live"
                return self._json(200, {"published": True})
        return self._json(404, {"error": "NOT_FOUND"})

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle))


def live_settings_values() -> dict:
    return {
        "eventbrite_execution_mode": "LIVE",
        "eventbrite_private_token": TOKEN,
        "eventbrite_organization_id": ORGANIZATION_ID,
        "eventbrite_organizer_id": ORGANIZER_ID,
        "eventbrite_currency": "USD",
    }
