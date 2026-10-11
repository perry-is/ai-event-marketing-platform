"""Operator smoke check for the live Eventbrite connector.

Default: READ-ONLY (token, current user, organization, organizer). No writes.
Optional draft write test only with EVENTBRITE_ENABLE_LIVE_WRITE_TEST=1: creates a clearly
synthetic, unlisted online DRAFT, reads it back, and deletes it. It never publishes.
Never prints the token or raw provider payloads.
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.eventbrite import (
    EventbriteClient,
    EventbriteError,
    PrivateTokenCredentials,
    configuration_blockers,
    live_readiness,
)


def main() -> int:
    settings = Settings()
    blockers = configuration_blockers(settings)
    if blockers:
        print(json.dumps({"read_only_smoke": "DEFERRED", "missing": blockers}))
        return 0
    client = EventbriteClient(PrivateTokenCredentials(settings))
    try:
        readiness = live_readiness(settings, client)
        report = {
            "read_only_smoke": "PASS" if readiness["checked_live"] else "FAIL",
            "status": readiness["status"],
            "reasons": readiness["reasons"],
            "organization_found": bool(readiness["organization_name"]),
            "organizer_found": bool(readiness["organizer_name"]),
            "live_write_test": "NOT RUN",
        }
        if os.environ.get("EVENTBRITE_ENABLE_LIVE_WRITE_TEST") == "1" and readiness["checked_live"]:
            report["live_write_test"] = _draft_write_test(settings, client)
    finally:
        client.close()
    print(json.dumps(report))
    return 0 if report["read_only_smoke"] == "PASS" else 1


def _draft_write_test(settings, client: EventbriteClient) -> str:
    start = (datetime.now(UTC) + timedelta(days=60)).replace(microsecond=0)
    body = {
        "event": {
            "name": {"html": "Event Marketing connector test (synthetic draft, safe to delete)"},
            "start": {"timezone": "UTC", "utc": start.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "end": {
                "timezone": "UTC",
                "utc": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "currency": settings.eventbrite_currency,
            "online_event": True,
            "organizer_id": settings.eventbrite_organizer_id,
            "listed": False,
        }
    }
    try:
        created = client.request(
            "POST", f"/organizations/{settings.eventbrite_organization_id}/events/", json_body=body
        )
        event_id = str(created["id"])
        remote = client.request("GET", f"/events/{event_id}/")
        verified = str(remote.get("id")) == event_id and remote.get("status") == "draft"
        client.request("DELETE", f"/events/{event_id}/")
        return "PASS (draft created, verified, deleted)" if verified else "FAIL (verification)"
    except EventbriteError as error:
        return f"FAIL ({error.category})"


if __name__ == "__main__":
    sys.exit(main())
