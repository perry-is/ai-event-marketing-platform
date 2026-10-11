"""Read-only Meetup JWT, member, Pro Network, and Group readiness smoke check."""

import json
import sys

from app.config import Settings
from app.meetup import (
    MeetupGraphQLClient,
    MeetupJWTTokenProvider,
    _configuration_blockers,
    live_readiness,
)


def main() -> int:
    settings = Settings()
    blockers = _configuration_blockers(settings)
    if blockers:
        print(json.dumps({"read_only_smoke": "DEFERRED", "missing": blockers}))
        return 0
    credentials = MeetupJWTTokenProvider(settings)
    client = MeetupGraphQLClient(credentials)
    try:
        readiness = live_readiness(settings, client)
        report = {
            "read_only_smoke": "PASS" if readiness["checked_live"] else "FAIL",
            "status": readiness["status"],
            "reasons": readiness["reasons"],
            "member_matched": bool(readiness["member_name"]),
            "network_found": bool(readiness["network_name"]),
            "group_found": bool(readiness["group_name"]),
            "live_write": "NOT RUN",
        }
    finally:
        client.close()
    print(json.dumps(report))
    return 0 if report["read_only_smoke"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
