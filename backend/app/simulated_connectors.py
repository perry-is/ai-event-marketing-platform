"""Offline simulated destination adapters and a test-only deterministic harness."""

import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Literal, Protocol

from fastapi import HTTPException

Outcome = Literal["SUCCESS", "FAILED_RETRYABLE", "FAILED_FINAL", "UNKNOWN"]
Verification = Literal["FOUND_MATCHING", "CONFIRMED_ABSENT", "INCONCLUSIVE"]

AUTO_DESTINATIONS = (
    "EVENTBRITE",
    "MEETUP",
    "LINKEDIN_ORGANIZATION",
    "INSTAGRAM",
)


@dataclass(frozen=True)
class AdapterDeclaration:
    destination_key: str
    execution_kind: Literal["AUTO", "MANUAL"]
    readiness_simulated: Literal["READY", "UNVERIFIED", "BLOCKED"]
    readiness_live: Literal["READY", "UNVERIFIED", "BLOCKED"]
    adapter_revision: str
    can_verify: bool


@dataclass(frozen=True)
class NormalizedResult:
    outcome: Literal[
        "PUBLISHED",
        "NOT_CREATED",
        "UNCERTAIN",
        "MANUAL_PACKAGE_READY",
        "MANUAL_ATTESTED",
        "BLOCKED",
        "VERIFICATION_RESULT",
    ]
    external_id: str | None = None
    external_url: str | None = None
    human_error: str | None = None
    error_category: str | None = None
    retryable: bool = False
    verification_status: str = "SIMULATED"
    evidence: dict | None = None
    verification_finding: Verification | None = None


class SimulationScenarioController:
    """In-memory outcomes injected only into an app configured with environment=test."""

    def __init__(self, environment: str):
        if environment != "test":
            raise RuntimeError("Simulation test outcomes require test environment")
        self._lock = threading.Lock()
        self._outcomes: dict[str, list[Outcome]] = {}
        self._verifications: dict[str, list[Verification]] = {}
        self._receipts: dict[str, tuple[str, str]] = {}

    def configure(
        self,
        outcomes: dict[str, list[Outcome]] | None = None,
        verifications: dict[str, list[Verification]] | None = None,
    ) -> None:
        allowed = set(AUTO_DESTINATIONS)
        if any(key not in allowed for key in (outcomes or {})):
            raise ValueError("Unsupported simulated destination")
        if any(key not in allowed for key in (verifications or {})):
            raise ValueError("Unsupported simulated destination")
        with self._lock:
            self._outcomes = {key: list(values) for key, values in (outcomes or {}).items()}
            self._verifications = {
                key: list(values) for key, values in (verifications or {}).items()
            }

    def next_outcome(self, destination: str) -> Outcome:
        with self._lock:
            outcomes = self._outcomes.get(destination, [])
            return outcomes.pop(0) if outcomes else "SUCCESS"

    def next_verification(self, destination: str) -> Verification | None:
        with self._lock:
            values = self._verifications.get(destination, [])
            return values.pop(0) if values else None

    def receipt(self, idempotency_key: str) -> tuple[str, str]:
        with self._lock:
            return self._receipts.get(idempotency_key, ("", ""))

    def save_receipt(self, idempotency_key: str, external_id: str, external_url: str) -> None:
        with self._lock:
            self._receipts.setdefault(idempotency_key, (external_id, external_url))

    def clear_receipt(self, idempotency_key: str) -> None:
        with self._lock:
            self._receipts.pop(idempotency_key, None)


class AutoAdapter(Protocol):
    declaration: AdapterDeclaration

    def validate(self, plan_entry: dict, runtime_mode: str) -> None: ...

    def render(self, plan_entry: dict) -> dict: ...

    def execute(self, plan_entry: dict, idempotency_key: str, attempt_number: int): ...

    def verify(self, plan_entry: dict, idempotency_key: str): ...


class SimulatedAutoAdapter:
    def __init__(self, destination_key: str, scenarios: SimulationScenarioController | None = None):
        if destination_key not in AUTO_DESTINATIONS:
            raise ValueError("Unsupported simulated AUTO destination")
        self.destination_key = destination_key
        self.scenarios = scenarios
        self.declaration = AdapterDeclaration(
            destination_key=destination_key,
            execution_kind="AUTO",
            readiness_simulated="READY",
            readiness_live="UNVERIFIED",
            adapter_revision="simulated-v1",
            can_verify=True,
        )

    def validate(self, plan_entry: dict, runtime_mode: str) -> None:
        if runtime_mode != "SIMULATED":
            raise HTTPException(409, "LIVE_EXECUTION_NOT_AVAILABLE")
        if plan_entry.get("destination_key") != self.destination_key:
            raise HTTPException(409, "ADAPTER_DESTINATION_MISMATCH")
        if plan_entry.get("execution_kind") != "AUTO":
            raise HTTPException(409, "ADAPTER_KIND_MISMATCH")
        if (
            not isinstance(plan_entry.get("approved_content"), str)
            or not plan_entry["approved_content"].strip()
        ):
            raise HTTPException(409, "DESTINATION_CONTENT_MISSING")
        graphic = plan_entry.get("approved_graphic")
        if graphic is not None and (
            not isinstance(graphic, dict)
            or not re.fullmatch(r"[0-9a-f-]{36}", str(graphic.get("asset_id", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(graphic.get("sha256", "")))
            or graphic.get("preview_url")
            != f"/api/campaign-versions/{plan_entry['campaign_version_id']}/graphic"
        ):
            raise HTTPException(409, "APPROVED_GRAPHIC_INVALID")

    def render(self, plan_entry: dict) -> dict:
        """Mechanical simulated mapping; copy remains byte-for-byte the approved string."""
        return {
            "destination_key": self.destination_key,
            "title": plan_entry["event_title"],
            "text": plan_entry["approved_content"],
            "graphic": plan_entry.get("approved_graphic"),
            "format_revision": self.declaration.adapter_revision,
        }

    def _receipt(self, idempotency_key: str) -> tuple[str, str]:
        suffix = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()[:16]
        external_id = f"simulated:{self.destination_key.lower()}:{suffix}"
        external_url = f"https://simulated.invalid/{self.destination_key.lower()}/{suffix}"
        if self.scenarios:
            self.scenarios.save_receipt(idempotency_key, external_id, external_url)
        return external_id, external_url

    def execute(
        self, plan_entry: dict, idempotency_key: str, attempt_number: int
    ) -> NormalizedResult:
        self.validate(plan_entry, "SIMULATED")
        rendered = self.render(plan_entry)
        outcome = self.scenarios.next_outcome(self.destination_key) if self.scenarios else "SUCCESS"
        if outcome == "SUCCESS":
            external_id, external_url = self._receipt(idempotency_key)
            return NormalizedResult(
                outcome="PUBLISHED",
                external_id=external_id,
                external_url=external_url,
                evidence={
                    "simulation": True,
                    "creation_receipt": "VERIFIED",
                    "approved_graphic": rendered.get("graphic"),
                },
            )
        if outcome in {"FAILED_RETRYABLE", "FAILED_FINAL"}:
            return NormalizedResult(
                outcome="NOT_CREATED",
                human_error="The simulated destination did not create this item.",
                error_category=(
                    "TRANSIENT_CONFIRMED_NO_EFFECT"
                    if outcome == "FAILED_RETRYABLE"
                    else "REMOTE_REJECTED"
                ),
                retryable=outcome == "FAILED_RETRYABLE",
                evidence={"simulation": True, "creation_receipt": "CONFIRMED_ABSENT"},
                verification_status="VERIFIED",
            )
        # This deterministic case models a timeout after the simulated remote may have accepted it.
        self._receipt(idempotency_key)
        return NormalizedResult(
            outcome="UNCERTAIN",
            human_error="The simulated request timed out after it may have been accepted.",
            error_category="AMBIGUOUS_OUTCOME",
            retryable=False,
            verification_status="INCONCLUSIVE",
            evidence={"simulation": True, "possible_remote_acceptance": True},
        )

    def verify(self, plan_entry: dict, idempotency_key: str) -> NormalizedResult:
        self.validate(plan_entry, "SIMULATED")
        finding = self.scenarios.next_verification(self.destination_key) if self.scenarios else None
        receipt = self.scenarios.receipt(idempotency_key) if self.scenarios else ("", "")
        if finding is None:
            finding = "FOUND_MATCHING" if receipt[0] else "INCONCLUSIVE"
        if finding == "FOUND_MATCHING":
            if not receipt[0]:
                # The test controller can provide a deterministic matching receipt on demand.
                receipt = self._receipt(idempotency_key)
            return NormalizedResult(
                outcome="VERIFICATION_RESULT",
                external_id=receipt[0],
                external_url=receipt[1],
                verification_status="SIMULATED",
                verification_finding="FOUND_MATCHING",
                evidence={"simulation": True, "matching_receipt": True},
            )
        if finding == "CONFIRMED_ABSENT":
            if self.scenarios:
                self.scenarios.clear_receipt(idempotency_key)
            return NormalizedResult(
                outcome="VERIFICATION_RESULT",
                error_category="TRANSIENT_CONFIRMED_NO_EFFECT",
                retryable=True,
                verification_status="VERIFIED",
                verification_finding="CONFIRMED_ABSENT",
                evidence={"simulation": True, "matching_receipt": False},
            )
        return NormalizedResult(
            outcome="VERIFICATION_RESULT",
            error_category="AMBIGUOUS_OUTCOME",
            retryable=False,
            verification_status="INCONCLUSIVE",
            verification_finding="INCONCLUSIVE",
            evidence={"simulation": True, "reconciliation": "INCONCLUSIVE"},
        )


class TestOnlyLiveModeAdapter:
    """No-network test double used to prove dispatch from an approved LIVE plan entry."""

    def __init__(self, destination_key: str, environment: str):
        if environment != "test" or destination_key != "EVENTBRITE":
            raise RuntimeError("The mocked LIVE-mode adapter is test-only and Eventbrite-only")
        self.destination_key = destination_key
        self.declaration = AdapterDeclaration(
            destination_key=destination_key,
            execution_kind="AUTO",
            readiness_simulated="BLOCKED",
            readiness_live="READY",
            adapter_revision="test-only-live-mode-mock-v1",
            can_verify=True,
        )
        self.network_calls = 0

    def validate(self, plan_entry: dict, runtime_mode: str) -> None:
        if runtime_mode != "LIVE":
            raise HTTPException(409, "APPROVED_CONNECTOR_MODE_UNAVAILABLE")
        # Reuse only the structural checks; this test double does not dispatch to that adapter.
        SimulatedAutoAdapter(self.destination_key).validate(plan_entry, "SIMULATED")

    def render(self, plan_entry: dict) -> dict:
        self.validate(plan_entry, "LIVE")
        return {
            "destination_key": self.destination_key,
            "title": plan_entry["event_title"],
            "text": plan_entry["approved_content"],
            "graphic": plan_entry.get("approved_graphic"),
            "format_revision": self.declaration.adapter_revision,
        }

    def execute(self, plan_entry: dict, idempotency_key: str, attempt_number: int):
        import hashlib

        self.validate(plan_entry, "LIVE")
        suffix = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()[:16]
        return NormalizedResult(
            outcome="PUBLISHED",
            external_id=f"test-live:eventbrite:{suffix}",
            external_url=f"https://eventbrite.mock.invalid/e/{suffix}",
            verification_status="TEST_MOCKED",
            evidence={
                "test_only_adapter": True,
                "network_calls": self.network_calls,
                "creation_receipt": "VERIFIED",
                "approved_graphic": plan_entry.get("approved_graphic"),
            },
        )

    def verify(self, plan_entry: dict, idempotency_key: str):
        import hashlib

        self.validate(plan_entry, "LIVE")
        suffix = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()[:16]
        return NormalizedResult(
            outcome="VERIFICATION_RESULT",
            external_id=f"test-live:eventbrite:{suffix}",
            external_url=f"https://eventbrite.mock.invalid/e/{suffix}",
            verification_status="TEST_MOCKED",
            verification_finding="FOUND_MATCHING",
            evidence={"test_only_adapter": True, "network_calls": self.network_calls},
        )


class FacebookGroupsManualAdapter:
    declaration = AdapterDeclaration(
        destination_key="FACEBOOK_GROUPS",
        execution_kind="MANUAL",
        readiness_simulated="READY",
        readiness_live="READY",
        adapter_revision="manual-package-v1",
        can_verify=False,
    )

    def validate(self, plan_entry: dict, runtime_mode: str) -> None:
        if runtime_mode not in {None, "SIMULATED"}:
            raise HTTPException(409, "LIVE_EXECUTION_NOT_AVAILABLE")
        if plan_entry.get("execution_kind") != "MANUAL":
            raise HTTPException(409, "ADAPTER_KIND_MISMATCH")
        if not plan_entry.get("approved_content"):
            raise HTTPException(409, "DESTINATION_CONTENT_MISSING")
        groups = plan_entry.get("selected_groups") or []
        if any(not group.get("url") or not group.get("display_name") for group in groups):
            raise HTTPException(409, "GROUP_TARGET_UNAVAILABLE")

    def produce_manual_package(self, plan_entry: dict) -> dict:
        self.validate(plan_entry, "SIMULATED")
        return {
            "package_version": 1,
            "campaign_version_id": plan_entry["campaign_version_id"],
            "event_title": plan_entry["event_title"],
            "destination": "Facebook Groups",
            "group_selection": (
                f"Share in the {len(plan_entry['selected_groups'])} approved "
                f"group{'s' if len(plan_entry['selected_groups']) != 1 else ''} below."
                if plan_entry.get("selected_groups")
                else "No group was selected; choose a group in Facebook manually."
            ),
            "selected_groups": [
                {
                    "group_record_id": group["group_record_id"],
                    "display_name": group["display_name"],
                    "url": group["url"],
                    "geography_label": group.get("geography_label"),
                    "member_count": group.get("member_count"),
                    "cautions": list(group.get("cautions", [])),
                }
                for group in plan_entry.get("selected_groups") or []
            ],
            "approved_copy": plan_entry["approved_content"],
            "creative_status": (
                "Approved campaign graphic included"
                if plan_entry.get("approved_graphic")
                else "No graphic generated"
            ),
            "graphic_asset_id": (
                plan_entry["approved_graphic"]["asset_id"]
                if plan_entry.get("approved_graphic")
                else None
            ),
            "graphic_sha256": (
                plan_entry["approved_graphic"]["sha256"]
                if plan_entry.get("approved_graphic")
                else None
            ),
            "graphic_preview_url": (
                plan_entry["approved_graphic"]["preview_url"]
                if plan_entry.get("approved_graphic")
                else None
            ),
            "creative_direction": plan_entry.get("creative_direction"),
            "instructions": (
                "Post the approved copy and graphic in each group above, then mark "
                "Facebook Groups complete."
                if plan_entry.get("selected_groups")
                else "Review the copy, choose appropriate Groups, and post manually."
            ),
            "simulation_label": "Manual action package; nothing was posted by this app.",
        }

    def execute(self, *_args, **_kwargs):
        raise RuntimeError("Facebook Groups are MANUAL and cannot auto-publish")


def build_simulated_adapters(
    scenarios: SimulationScenarioController | None = None,
) -> dict[str, object]:
    adapters: dict[str, object] = {
        key: SimulatedAutoAdapter(key, scenarios) for key in AUTO_DESTINATIONS
    }
    adapters["FACEBOOK_GROUPS"] = FacebookGroupsManualAdapter()
    return adapters


class DestinationAdapterRegistry:
    """Server-owned adapter/mode lookup. Plan entries, not browser fields, select adapters."""

    def __init__(
        self,
        settings,
        scenarios: SimulationScenarioController | None = None,
        live_factory=None,
        meetup_live_factory=None,
        linkedin_live_factory=None,
        instagram_live_factory=None,
    ):
        self.environment = settings.environment
        self.live_accounts: dict[str, dict] = {}
        self.live_targets: dict[str, dict] = {}
        self.runtime_modes = {
            "EVENTBRITE": settings.eventbrite_execution_mode,
            "MEETUP": settings.meetup_execution_mode,
            "LINKEDIN_ORGANIZATION": settings.linkedin_organization_execution_mode,
            "INSTAGRAM": settings.instagram_execution_mode,
        }
        self.adapters: dict[tuple[str, str, str | None], object] = {}
        for destination in AUTO_DESTINATIONS:
            adapter = SimulatedAutoAdapter(destination, scenarios)
            self.adapters[(destination, "AUTO", "SIMULATED")] = adapter
            mode = self.runtime_modes[destination]
            if mode == "LIVE" and destination == "EVENTBRITE":
                live = live_factory() if live_factory else None
                if live is not None:
                    self.adapters[(destination, "AUTO", "LIVE")] = live
                    if live.account and not live.blocker():
                        self.live_accounts[destination] = live.account
                elif self.environment == "test":
                    # WP1I-A no-network double, used only when no Eventbrite HTTP mock is wired.
                    self.adapters[(destination, "AUTO", "LIVE")] = TestOnlyLiveModeAdapter(
                        destination, self.environment
                    )
            elif mode == "LIVE" and destination == "MEETUP":
                live = meetup_live_factory() if meetup_live_factory else None
                if live is not None:
                    self.adapters[(destination, "AUTO", "LIVE")] = live
                    self.live_targets[destination] = dict(live.target)
            elif mode == "LIVE" and destination == "LINKEDIN_ORGANIZATION":
                live = linkedin_live_factory() if linkedin_live_factory else None
                if live is not None:
                    self.adapters[(destination, "AUTO", "LIVE")] = live
                    self.live_targets[destination] = dict(live.target)
            elif mode == "LIVE" and destination == "INSTAGRAM":
                live = instagram_live_factory() if instagram_live_factory else None
                if live is not None:
                    self.adapters[(destination, "AUTO", "LIVE")] = live
                    self.live_targets[destination] = dict(live.target)
        self.adapters[("FACEBOOK_GROUPS", "MANUAL", None)] = FacebookGroupsManualAdapter()

    def for_entry(self, entry: dict):
        mode = entry.get("runtime_mode") if entry.get("execution_kind") == "AUTO" else None
        return self.adapters.get((entry.get("destination_key"), entry.get("execution_kind"), mode))

    def current_mode(self, destination: str) -> str | None:
        if destination == "FACEBOOK_GROUPS":
            return None
        return self.runtime_modes.get(destination)

    def readiness(self, entry: dict) -> tuple[str, str | None]:
        adapter = self.for_entry(entry)
        kind = entry.get("execution_kind")
        destination = entry.get("destination_key")
        if kind == "MANUAL":
            if destination != "FACEBOOK_GROUPS" or adapter is None:
                return "BLOCKED", "MANUAL_PACKAGE_UNAVAILABLE"
            try:
                adapter.validate(entry, None)
            except HTTPException as error:
                return "BLOCKED", str(error.detail)
            return "READY", None
        mode = entry.get("runtime_mode")
        if mode not in {"SIMULATED", "LIVE"}:
            return "BLOCKED", "APPROVED_CONNECTOR_MODE_UNAVAILABLE"
        if mode == "LIVE" and self.current_mode(destination) != "LIVE":
            return "BLOCKED", "APPROVED_CONNECTOR_MODE_UNAVAILABLE"
        if adapter is None:
            return "BLOCKED", "APPROVED_CONNECTOR_MODE_UNAVAILABLE"
        declared = getattr(adapter.declaration, f"readiness_{mode.lower()}")
        # OAuth-backed live connectors are registered statically, while their account
        # connection and authorization can change at runtime. Their validate() method is
        # the authoritative readiness check and must run on every request.
        if declared != "READY" and not getattr(adapter, "dynamic_readiness", False):
            blocker = getattr(adapter, "blocker", None)
            return "BLOCKED", (blocker() if blocker else None) or "DESTINATION_NOT_READY"
        try:
            adapter.validate(entry, mode)
        except HTTPException as error:
            return "BLOCKED", str(error.detail)
        return "READY", None
