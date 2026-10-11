"""Stateful HTTP-level Meetup test provider; never contacts Meetup."""

import copy
import json
from urllib.parse import parse_qs

import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.meetup import API_URL, TOKEN_URL

MEMBER_ID = "81234567"
NETWORK_URLNAME = "event-marketing-network"
GROUP_URLNAME = "example-business-network"
GROUP_ID = "81234001"
VENUE_ID = "81234002"


def write_test_private_key(path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


class FakeMeetup:
    def __init__(self):
        self.member_id = MEMBER_ID
        self.network_urlname = NETWORK_URLNAME
        self.group_urlname = GROUP_URLNAME
        self.group_status = "ACTIVE"
        self.calls = []
        self.events = {}
        self.created = 0
        self.updated = 0
        # Remote identities stay unique across per-test state resets because the
        # browser harness shares one PostgreSQL database for the whole run.
        self.next_event_id = 920000001
        self.assertions = []
        self.timeout_after_create = False
        self.top_error_next = None
        self.payload_error_next = None
        self.malformed_next = False
        self.venues = [
            {
                "id": VENUE_ID,
                "name": "Aurora Event Center",
                "address": "123 Main St",
                "city": "Aurora",
                "state": "CO",
                "country": "US",
            }
        ]

    def client(self):
        return httpx2.Client(transport=httpx2.MockTransport(self.handle))

    def reset(self):
        self.calls.clear()
        self.events.clear()
        self.created = 0
        self.updated = 0
        self.timeout_after_create = False
        self.top_error_next = None
        self.payload_error_next = None
        self.malformed_next = False

    def handle(self, request):
        if request.url == httpx2.URL(TOKEN_URL):
            form = parse_qs(request.content.decode())
            assertion = form.get("assertion", [""])[0]
            self.assertions.append(assertion)
            if form.get("grant_type") != ["urn:ietf:params:oauth:grant-type:jwt-bearer"]:
                return httpx2.Response(400, json={"error": "invalid_request"})
            return httpx2.Response(
                200,
                json={"access_token": "synthetic-meetup-token", "expires_in": 3600},
            )
        if request.url == httpx2.URL(API_URL):
            if request.headers.get("Authorization") != "Bearer synthetic-meetup-token":
                return httpx2.Response(401, json={"errors": [{"message": "unauthorized"}]})
            body = json.loads(request.content)
            name = body.get("operationName")
            variables = body.get("variables") or {}
            self.calls.append(
                {
                    "operation": name,
                    "query": body.get("query"),
                    "variables": copy.deepcopy(variables),
                }
            )
            if self.malformed_next:
                self.malformed_next = False
                return httpx2.Response(200, content=b"not-json")
            if self.top_error_next:
                code = self.top_error_next
                self.top_error_next = None
                return httpx2.Response(
                    200,
                    json={
                        "data": None,
                        "errors": [{"message": "synthetic error", "extensions": {"code": code}}],
                    },
                )
            data = self._operation(name, variables)
            if self.payload_error_next:
                code = self.payload_error_next
                self.payload_error_next = None
                key = next(iter(data))
                data[key]["errors"] = [{"message": "synthetic payload error", "code": code}]
            return httpx2.Response(200, json={"data": data})
        return httpx2.Response(404, json={"errors": [{"message": "not found"}]})

    def _operation(self, name, variables):
        if name == "MeetupSelfReadiness":
            return {"self": {"id": self.member_id, "name": "Event Marketing"}}
        if name == "MeetupNetworkReadiness":
            urlname = variables["urlname"]
            network = (
                {
                    "urlname": self.network_urlname,
                    "name": "Example Network",
                    "status": "ACTIVE",
                }
                if urlname == self.network_urlname
                else None
            )
            return {"proNetwork": network}
        if name == "MeetupGroupReadiness":
            urlname = variables["urlname"]
            group = (
                {
                    "id": GROUP_ID,
                    "name": "Example Business Network",
                    "urlname": self.group_urlname,
                    "status": self.group_status,
                    "proNetwork": {
                        "urlname": self.network_urlname,
                        "name": "Example Network",
                    },
                    "venues": {"edges": [{"node": copy.deepcopy(v)} for v in self.venues]},
                }
                if urlname == self.group_urlname
                else None
            )
            return {"groupByUrlname": group}
        if name == "MeetupEventRead":
            return {"event": copy.deepcopy(self.events.get(str(variables["id"])))}
        if name == "MeetupEventCreate":
            values = variables["input"]
            if values["groupUrlname"] != self.group_urlname:
                return {
                    "createEvent": {
                        "event": None,
                        "errors": [{"message": "invalid group", "code": "BAD_USER_INPUT"}],
                    }
                }
            self.created += 1
            event_id = str(self.next_event_id)
            self.next_event_id += 1
            event = self._new_event(event_id, values, "DRAFT")
            self.events[event_id] = event
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise httpx2.ReadTimeout("synthetic timeout after accepted create")
            return {"createEvent": {"event": self._project(event), "errors": []}}
        if name == "MeetupEventEdit":
            values = variables["input"]
            event_id = str(values["eventId"])
            event = self.events.get(event_id)
            if event is None:
                return {
                    "editEvent": {
                        "event": None,
                        "errors": [{"message": "missing", "code": "NOT_FOUND"}],
                    }
                }
            self.updated += 1
            for source, target in (
                ("title", "title"),
                ("description", "description"),
                ("startDateTime", "dateTime"),
                ("duration", "duration"),
            ):
                if source in values:
                    event[target] = values[source]
            if values.get("publishStatus") == "PUBLISHED":
                event["status"] = "ACTIVE"
            elif values.get("publishStatus") == "DRAFT":
                event["status"] = "DRAFT"
            return {"editEvent": {"event": self._project(event), "errors": []}}
        if name == "MeetupEventPhotoCreate":
            raise AssertionError(
                "WP1J must not create photo placeholders before upload contract verification"
            )
        raise AssertionError(f"Unexpected Meetup operation: {name}")

    @staticmethod
    def _new_event(event_id, values, status):
        group_urlname = values.get("groupUrlname") or "example-business-network"
        return {
            "id": event_id,
            "title": values["title"],
            "eventUrl": f"https://www.meetup.com/{group_urlname}/events/{event_id}",
            "description": values["description"],
            "dateTime": values["startDateTime"],
            "duration": values["duration"],
            "eventType": "PHYSICAL",
            "status": status,
            "venue": {
                "id": values.get("venueId") or VENUE_ID,
                "name": "Aurora Event Center",
                "address": "123 Main St",
                "city": "Aurora",
                "state": "CO",
                "country": "US",
            },
            "group": {
                "id": GROUP_ID,
                "name": "Example Business Network",
                "urlname": group_urlname,
            },
        }

    @staticmethod
    def _project(event):
        return {
            key: copy.deepcopy(event.get(key))
            for key in (
                "id",
                "title",
                "status",
                "eventUrl",
                "description",
                "dateTime",
                "duration",
                "eventType",
                "venue",
                "group",
            )
        }
