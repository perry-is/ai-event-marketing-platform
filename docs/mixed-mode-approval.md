# Mixed-mode approval and destination plans

WP1I-A replaces the earlier global runtime-mode assumption. One approval can authorize an exact CampaignVersion while its selected destinations follow different execution treatments: Eventbrite AUTO/LIVE, Meetup, LinkedIn organization and Instagram AUTO/SIMULATED, and Facebook Groups MANUAL. This package adds no real live connector; the only LIVE-shaped implementation is an isolated test adapter that makes no network calls.

## Two independent hashes

The CampaignVersion content hash represents reviewed business content: the Event snapshot and its source facts, copy, graphics, selected content and other material campaign intent. It does not include connector runtime mode or the process environment. Changing only an execution mode leaves the CampaignVersion hash unchanged.

At approval preview, the backend builds a destination plan from the exact current CampaignVersion, legitimate target choices, fixed product rules and trusted server configuration. The plan is canonicalized and hashed independently. Its hash covers destination and target identity, AUTO/MANUAL treatment, AUTO SIMULATED/LIVE mode, approved per-destination content/asset references and the exact Facebook Group target set. It excludes database row IDs that are not stable target identity, timestamps, readiness messages, connector health, UI labels, request IDs and preview URLs. Target/group collections are sorted by stable identity for hashing; values within an approved content payload retain their defined order.

Approval persists both hashes and the complete plan snapshot. Equivalent plans produce the same destination-plan hash; changed execution intent produces a different hash. Approval rows may repeat a prior `(CampaignVersion, plan hash)` after an intervening plan has superseded it: the prior immutable approval remains history, but it cannot be reactivated as new permission.

## Plan entry semantics

Each AUTO entry has a `runtime_mode` of `SIMULATED` or `LIVE`. Each MANUAL entry has no runtime mode. The Facebook Groups entry is one MANUAL package with the exact selected Group identities and chosen URLs inside it, and one manual completion action. No DestinationExecution is created for each individual Group.

Execution treatment and runtime mode are separate dimensions. `AUTO` versus `MANUAL` says who/what acts. `SIMULATED` versus `LIVE` says how an AUTO adapter behaves. Exclusion/content-only information can remain visible in the plan response, but does not create an execution unit.

## Server-owned mode and readiness

The client submits the CampaignVersion ID/hash, its expected campaign revision, `expected_destination_plan_hash` returned by the preview, and any permitted target-selection intent. The expected hash is compare-only: the browser cannot submit plan contents or `runtime_mode`. Under the existing Event/Campaign transaction and lock, the backend rebuilds and hashes the authoritative plan; if it differs from the preview, approval returns `APPROVAL_PLAN_CHANGED` (HTTP 409), creates no Approval, and the UI refreshes the preview for explicit review. The backend derives per-destination modes from `EMP_EVENTBRITE_EXECUTION_MODE`, `EMP_MEETUP_EXECUTION_MODE`, `EMP_LINKEDIN_ORGANIZATION_EXECUTION_MODE` and `EMP_INSTAGRAM_EXECUTION_MODE`. All default to `SIMULATED`; Facebook Groups is fixed MANUAL. Invalid mode values fail configuration validation. A mode with no supported adapter is BLOCKED, never downgraded to simulation.

Before approval, deterministic local readiness is checked for every included entry. This does not require all external services to be online. Safe server-derived target labels are displayed for user review but are excluded from the hash when cosmetic. Deterministically known, accountable limitations such as an immutable already-published Instagram post or unsupported LinkedIn media replacement are displayed as unable to publish automatically; eligible outcomes may still be approved for explicit later accounting under existing policy. Other blocked selected destinations remain not ready and prevent approval until intent is corrected or the destination is explicitly excluded and reviewed.

## Historical plan execution

Start, retry and reconciliation load and verify the stored Approval, CampaignVersion content hash and stored destination-plan hash. They dispatch from the approved entries; they never rebuild an old Approval from current settings.

If an Approval says Eventbrite LIVE and the server later only has SIMULATED capability/configuration, that entry is BLOCKED. The system does not create a simulated receipt. If an Approval says Meetup SIMULATED and a future deployment gains LIVE Meetup support, retries of the old Approval stay SIMULATED. A changed server-derived plan requires a new Approval. Approval and execution remain separate durable actions; a failure starting execution does not erase approval.

## Migration and compatibility

Migration `0007_mixed_mode_approval` converts historical schema-version-1 approval plans to version 2. The old global `SIMULATED` value is copied onto each historical AUTO entry, removed from MANUAL entries, and the destination-plan hash is calculated from that frozen historical plan. Existing destination executions receive the matching approval plan hash; historical Facebook package rows become MANUAL with a null runtime mode. Existing approval rows retain their old global `runtime_mode` and `approved_plan_hash` columns as nullable legacy metadata, but runtime code never uses them as authorization.

The downgraded schema can represent only the previous all-SIMULATED model. Downgrade checks first and refuses mixed LIVE plans or approval histories that cannot fit the old uniqueness rules. It restores legacy hashes and fields for representable all-SIMULATED history. It never silently collapses a mixed approval.

The authenticated test-only fixtures can select fixed Eventbrite test modes to prove mixed dispatch and configuration drift. These routes are mounted only in `environment=test`, accept no arbitrary destination/mode value, and cannot contact Eventbrite. Production server configuration, not the browser, is the authority.
