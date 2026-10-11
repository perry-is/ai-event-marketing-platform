"""Explicitly gated synthetic Meetup DRAFT create/read/delete test; never publishes."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.meetup import (
    EVENT_CREATE,
    EVENT_READ,
    GROUP_READ,
    MeetupGraphQLClient,
    MeetupJWTTokenProvider,
    _configuration_blockers,
    live_readiness,
    plan_context,
)

DELETE_DRAFT = """mutation MeetupDeleteSyntheticDraft($input: DeleteEventInput!) {
  deleteEvent(input: $input) { success }
}"""
TEST_TITLE = "TEST - Event Marketing Meetup Connector - DELETE ME"


def _report(status: str, **fields) -> None:
    print(json.dumps({"draft_write_test": status, **fields}))


def main() -> int:
    if os.environ.get("MEETUP_ENABLE_LIVE_WRITE_TEST") != "1":
        _report("NOT RUN", reason="explicit write opt-in is off")
        return 0
    if os.environ.get("MEETUP_ENABLE_VERIFIED_DRAFT_CLEANUP") != "1":
        _report("NOT RUN", reason="explicit cleanup verification opt-in is off")
        return 0

    settings = Settings()
    blockers = _configuration_blockers(settings)
    if blockers:
        _report("NOT RUN", missing=blockers)
        return 1
    credentials = MeetupJWTTokenProvider(settings)
    client = MeetupGraphQLClient(credentials)
    event_id = None
    cleanup = "NOT ATTEMPTED"
    try:
        readiness = live_readiness(settings, client)
        if not readiness["checked_live"]:
            _report("NOT RUN", reason=readiness["reasons"] or ["MEETUP_NOT_READY"])
            return 1

        target = plan_context(settings)
        group_data = client.execute(
            "MeetupGroupReadiness", GROUP_READ, {"urlname": target["group_urlname"]}
        )
        group = group_data.get("groupByUrlname") or {}
        venues = [
            edge.get("node")
            for edge in (group.get("venues") or {}).get("edges", [])
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
        ]
        venue = next((item for item in venues if item.get("id")), None)
        if not venue:
            _report("NOT RUN", reason="configured Group has no verified existing venue")
            return 1

        start = (datetime.now(UTC) + timedelta(days=90)).replace(microsecond=0)
        input_data = {
            "groupUrlname": target["group_urlname"],
            "title": TEST_TITLE,
            "description": "Synthetic connector verification draft. Delete after checking.",
            "startDateTime": start.isoformat(timespec="seconds"),
            "duration": "PT1H",
            "venueId": str(venue["id"]),
            "publishStatus": "DRAFT",
            "selfRsvp": False,
        }
        try:
            created = client.execute(
                "MeetupEventCreate", EVENT_CREATE, {"input": input_data}, mutation=True
            )
        except Exception:
            _report(
                "UNKNOWN",
                reason="create request outcome may be uncertain; do not retry this write",
                external_event_id=None,
                cleanup="NOT POSSIBLE WITHOUT A RETURNED ID",
            )
            return 1
        payload = created.get("createEvent") or {}
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict) or not event.get("id"):
            _report(
                "FAIL", reason="Meetup did not return a confirmed draft ID", external_event_id=None
            )
            return 1
        event_id = str(event["id"])

        remote = client.execute("MeetupEventRead", EVENT_READ, {"id": event_id}).get("event")
        safe_to_cleanup = bool(
            isinstance(remote, dict)
            and str(remote.get("id")) == event_id
            and remote.get("title") == TEST_TITLE
            and remote.get("status") == "DRAFT"
            and (remote.get("group") or {}).get("urlname") == target["group_urlname"]
        )
        verified = bool(
            safe_to_cleanup
            and not payload.get("errors")
            and remote.get("description") == input_data["description"]
            and str((remote.get("venue") or {}).get("id")) == str(venue["id"])
        )
        if not safe_to_cleanup:
            cleanup = "NOT ATTEMPTED; draft identity/status did not match"
        else:
            try:
                deleted = client.execute(
                    "MeetupDeleteSyntheticDraft",
                    DELETE_DRAFT,
                    {"input": {"eventId": event_id, "removeFromCalendar": True}},
                    mutation=True,
                )
                cleanup = (
                    "DELETED"
                    if (deleted.get("deleteEvent") or {}).get("success") is True
                    else "FAILED"
                )
                if cleanup == "DELETED":
                    remaining = client.execute("MeetupEventRead", EVENT_READ, {"id": event_id}).get(
                        "event"
                    )
                    cleanup = "DELETED AND VERIFIED" if remaining is None else "FAILED VERIFICATION"
            except Exception:
                cleanup = "FAILED"

        report_status = "PASS" if verified and cleanup == "DELETED AND VERIFIED" else "FAIL"
        _report(
            report_status,
            external_event_id=event_id,
            draft_verified=verified,
            cleanup=cleanup,
            published=False,
            photo_upload="NOT ATTEMPTED",
        )
        return 0 if report_status == "PASS" else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
