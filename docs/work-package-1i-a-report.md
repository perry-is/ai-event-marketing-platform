# Work Package 1I-A report

**Status: COMPLETE.** This package corrects approval and destination-plan semantics so one approval can authorize mixed per-destination execution modes. It implements no production network connector and does not resume Eventbrite publishing.

## 1. Previous architecture

The prior model stored one global `runtime_mode` on Approval. Although individual DestinationExecution rows also had a mode, the mode came from the global approval and could not safely express Eventbrite LIVE, other AUTO destinations SIMULATED, and Facebook Groups MANUAL together.

## 2. Exact conflict

The global approval mode conflated distribution intent across unrelated destinations. Recomputing it at execution time would either change an old approval after configuration changes or silently select the wrong connector. No Eventbrite network behavior had been implemented at the pause point.

## 3. Final approval model

Approval now binds the exact CampaignVersion ID and content hash plus an immutable destination-plan snapshot and independent destination-plan hash. It also records the approver, timestamp, and source Event revision. Approval remains immutable historical evidence; execution start is a separate action.

## 4. Destination-plan representation

Plan schema version 2 contains sorted destination entries, exclusions, and content-only items. Entries include stable destination/target identity, `AUTO` or `MANUAL` treatment, approved content and other execution-significant target context. An AUTO entry includes its server-derived `SIMULATED` or `LIVE` mode. A MANUAL entry has no runtime mode. Facebook Groups remains one manual package with the exact selected group set.

## 5–6. Treatment and runtime mode

`AUTO` / `MANUAL` specifies who or what carries out a destination action. `SIMULATED` / `LIVE` specifies how an AUTO action is carried out. These are separate dimensions. Facebook Groups is fixed MANUAL. A manual package cannot be dispatched through an automatic adapter.

## 7. Server configuration

Per-destination server settings are validated against `SIMULATED` / `LIVE`. Eventbrite, Meetup, LinkedIn organization, and Instagram default to SIMULATED. Unsupported LIVE combinations fail readiness closed. Only a test-environment Eventbrite LIVE-shaped adapter exists; it returns synthetic results and performs no network call. Server configuration, never browser input, proposes the plan.

## 8. Approval request contract

The approval request contains the exact version ID/hash, campaign revision, and existing acknowledgement fields. It has no global runtime-mode or client-supplied destination-plan-hash field; strict request validation rejects either field if supplied. The backend constructs, validates, canonicalizes, hashes, and persists the proposed plan.

## 9. CampaignVersion hash

CampaignVersion content hashing remains unchanged and contains no runtime mode or deployment setting. A mode-only change therefore leaves its content hash unchanged.

## 10. Destination-plan hash

A deterministic SHA-256 hash is computed from a canonical projection of the destination plan. It includes destination and target identity, treatment, AUTO runtime mode, approved content, selected Facebook Group targets, and other significant target values. It excludes timestamps, transient readiness/health, request IDs, preview URLs, UI labels, and incidental array order. Tests prove equivalent plans hash equally, while mode or target changes alter the hash.

## 11. Persistence

Approval stores the plan snapshot/hash. DestinationExecution stores the same plan hash and its exact approved plan entry, with the authorized mode copied from that entry (null for MANUAL). The old global Approval `runtime_mode` and `approved_plan_hash` columns remain nullable legacy metadata and no longer authorize execution. The old one-approval-per-CampaignVersion uniqueness rule is removed so a changed distribution plan can receive a fresh approval without modifying prior approvals.

## 12. Historical migration

Migration `0007_mixed_mode_approval` adds and backfills destination-plan hashes, normalizes each pre-existing simulation-only plan to schema version 2 with AUTO/SIMULATED entries and MANUAL Facebook Groups without a mode, updates execution identity/idempotency fields, and retains old columns as non-authoritative nullable history. Historical plans are not derived from current configuration. The round-trip test confirms a populated historical Approval retains the same simulated/manual meaning. Downgrade refuses histories that cannot be represented by the old all-simulated schema.

## 13. Execution start

Execution start loads the selected Approval and its exact CampaignVersion, validates both the CampaignVersion content hash and stored destination-plan hash, checks existing active/material-Event authorization, and creates or loads executions from the stored plan. It does not rebuild that Approval from current settings.

## 14. Connector selection

Registry lookup is keyed by destination, treatment, and the plan entry's runtime mode. A stored LIVE mode with current settings now SIMULATED is BLOCKED; it is never changed to simulated. A stored SIMULATED entry continues through its simulator if the server later enables LIVE. An unavailable approved adapter blocks honestly. Tests use the synthetic Eventbrite LIVE-shaped adapter only in the test environment.

## 15. Retry behavior

Retries and reconciliation use the same persisted execution, Approval, CampaignVersion, destination-plan hash, plan entry, and approved mode. A retry cannot adopt a newer server mode or rerun a successful sibling destination.

## 16. Facebook Groups

The existing single MANUAL execution package and one human completion action remain intact. The approved package retains the exact selected group targets and has a null runtime mode. No per-group DestinationExecution records or automatic Facebook calls were introduced.

## 17. Frontend

The approval preview displays server-derived language: “Will publish live,” “Practice mode,” or the existing manual wording, plus readiness blockers. Approval JSON contains no mode/hash authority. Execution and history distinguish a live-labeled result from a simulated result; the test-only mock is explicitly identified. The responsive mixed-mode E2E covers desktop and iPhone-sized viewports.

## 18. Backend tests

**PASS: 183/183** full PostgreSQL-backed backend tests. Coverage includes deterministic plan hashing, strict request rejection, mixed approval/dispatch, unsupported-mode blocking, configuration drift, fresh approvals after plan changes, immutable historical authorization, retry mode preservation, and migration/backfill behavior.

## 19. Frontend tests

**PASS: 38/38** Vitest tests. Frontend lint and TypeScript project checks pass. API type generation completed and reflects removal of browser-authoritative runtime mode.

## 20. Playwright

**PASS: 22/22** across desktop Chrome and iPhone 13-sized Chromium (11 scenarios on each). Includes the mixed-mode plan/dispatch, mode-drift blocking, and historical all-simulated behavior. All destination behavior was mocked or simulated; no live external network calls were made.

## 21. Migration checks

**PASS.** The full suite's empty-database test applies the entire chain, repeats upgrade, and exercises downgrade/re-upgrade. A focused WP1I-A migration run also passed: **2/2** tests, including populated historical Approval/Execution normalization and round trip.

## 22. Security review

Approval and execution routes remain authenticated and mutation-protected. The strict request model rejects browser-supplied runtime mode and plan hash. Test-only configuration endpoints are available only in the test environment and provide fixed actions rather than accepting arbitrary mode strings. No credentials or tokens were added. The changed-file scan found only blank environment placeholders and test/config identifiers; no secret values. Source/reference integrity validation passed.

## 23. Files created or modified

Modified: `.env.example`, `README.md`; backend `app/config.py`, `app/execution_routes.py`, `app/execution_service.py`, `app/main.py`, `app/models.py`, `app/simulated_connectors.py`; backend tests `test_campaigns.py`, `test_config.py`, `test_execution.py`, `test_facebook_groups.py`, `test_graphics.py`; frontend `src/api.generated.ts`, `src/campaigns.tsx`, `src/campaigns.test.tsx`, `e2e/campaign-execution.spec.ts`; and architecture documents listed below.

Created: `backend/migrations/versions/0007_mixed_mode_approval.py`, `backend/tests/test_mixed_mode_approval.py`, `frontend/e2e/campaign-mixed-mode.spec.ts`, `docs/mixed-mode-approval.md`, and this report.

## 24. Architecture documents updated

Updated `docs/approval-and-simulated-execution.md`, `docs/architecture.md`, `docs/campaign-graphics.md`, `docs/campaign-versioning.md`, `docs/connector-contract.md`, `docs/data-model.md`, `docs/domain-model.md`, `docs/execution-safety.md`, and `docs/platform-capability-matrix.md` to remove the global-mode authority and describe the two-hash approval boundary.

## 25. Known limitations

The only LIVE-shaped adapter is an isolated test adapter; it is not a publisher and cannot make a network request. The current implementation does not perform destination-specific live account readiness probes. A production LIVE plan for unsupported destinations is blocked.

## 26. Deferred Eventbrite work

All real Eventbrite work remains deferred to resumed WP1I: private-token credentials, read-only readiness, organization/organizer setup, deterministic payload rendering, external resource binding, venue/ticket/media operations, draft-first create/update, publish/verify, and safe reconciliation. No other live destination connector, OAuth flow, credential, or Eventbrite resource-binding table was added.

## Final validation record

- Backend: `uv run pytest -q` — 183 passed.
- Frontend: `npm test` — 38 passed; lint/type generation/build passed; `npm run format:check` passed.
- Playwright: `npm run test:e2e` — 22 passed across desktop and mobile.
- Migrations: empty full-chain repeat upgrade/downgrade and populated historical Approval round trip passed.
- Python: Ruff check and format check passed.
- Security/dependencies: `npm audit` found 0 vulnerabilities; pip-audit found no known vulnerabilities among PyPI-resolvable installed packages (the local project itself is not published on PyPI).
- Repository: `git diff --check` passed; source-integrity validator passed; no source/reference files were modified.
