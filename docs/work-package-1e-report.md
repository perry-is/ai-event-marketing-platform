# Work Package 1E — Approval and simulated campaign execution

**Status: WP1E COMPLETE / ACCEPTED.** WP1E adds exact-CampaignVersion approval, deterministic simulated per-destination execution, manual Facebook Groups handling, retries, reconciliation, truthful completion, and campaign distribution history. It does not call destination APIs or change the deterministic WP1D campaign generator.

## Implementation

### 1. Summary

The simulated Phase 1 flow now continues from CampaignVersion review through exact-version approval, separate execution start, independent destination outcomes, retry/reconciliation/manual actions, and persistent history. Runtime mode is fixed to `SIMULATED` for this package.

### 2–5. Approval, version binding, separation, and destination plan

Alembic revision `0004_approval_execution` adds immutable Approval records. Each binds one CampaignVersion ID and content hash to an immutable approved destination-plan hash, source Event revision, approving application user, timestamp, and `SIMULATED` mode. Campaign stores only the active Approval pointer; historical approvals remain queryable. Version N approval never authorizes N+1.

The UI saves approval before offering a separate “Start simulated distribution” operation. An execution-start failure leaves the durable authorization resumable. The server validates the current, non-stale version and plan before approval and again before execution. The approved destination plan cannot expand after approval.

Eventbrite, Meetup, LinkedIn organization, and Instagram are deterministic AUTO simulators. Facebook Groups is MANUAL and receives a finished copy package without selected Groups. Facebook Page stays excluded. The prompt listed LinkedIn personal among conceptual targets, but the reviewed platform-capability matrix keeps Event Marketing access/routing unverified and nonselectable. Accordingly, personal LinkedIn copy remains reviewable but is marked content-only and excluded from execution.

### 6–16. Adapters, persistence, safety, and history

The simulated adapter boundary declares execution kind/readiness, validates an approved plan entry, performs only mechanical rendering, executes deterministic outcomes, verifies ambiguous results, and returns normalized outcomes. No real network publisher or destination OAuth was added.

Persistence adds `approvals`, `destination_executions`, `execution_attempts`, and append-only `audit_events`, plus Campaign active-approval and execution lifecycle states. The full relationships, constraints, triggers, indexes, and behavior are documented in [approval-and-simulated-execution.md](approval-and-simulated-execution.md). Each destination/target/version/mode has a stable SHA-256 idempotency identity. A retry creates a new attempt under one existing execution and does not touch successful siblings. Running attempts are uniquely constrained.

UNKNOWN is preserved as unresolved until reconciliation verifies success, confirms absence, or remains inconclusive. It cannot be blindly retried. Explicit acknowledgement of uncertainty is possible but is labeled as acknowledged uncertainty. Final failures similarly require explicit accounting. Facebook Groups receives approved copy, Event identity, a group-selection reminder, and text-only creative direction; one human completion action records user/time without a per-Group loop.

Material Event changes withdraw active authorization only when the material Event projection changes. Existing versions, approvals, destination results, and audit history remain. Internal/administrative notes do not invalidate approval. New campaign revisions withdraw the former active authorization and require fresh approval. Unresolved UNKNOWN or in-progress work blocks unsafe Event changes/successor revisions.

`COMPLETE` is derived only after all approved targets are resolved or explicitly accounted for. The outcome distinguishes successful completion, manual actions, acknowledged failures, and acknowledged uncertainty. An append-only audit action records the completion timestamp, shown in the distribution summary/UI.

### 17–21. Frontend, tests, and migrations

The Campaign review now shows the exact simulated destination plan and content-only exclusions before approval. The execution panel provides destination-specific status, per-attempt history, retry only for eligible failures, verification for UNKNOWN, a Facebook Groups copy package/completion control, approval history, and the completion outcome/time. The layout remains responsive.

Automated backend coverage exercises approval/auth boundaries, durable approval if start fails, exact-version authorization, independent success/failure, retries, duplicate/concurrent start, manual-only enforcement, UNKNOWN/reconciliation, failure acknowledgement, Event invalidation, successor version safety, immutable persistence, test-control isolation, and empty-database migration upgrade/downgrade/re-upgrade.

Final verification results:

| Check | Result |
| --- | --- |
| PostgreSQL-backed backend suite | **112 passed** |
| Empty database migration chain, repeat upgrade, downgrade, re-upgrade | **Passed** in the backend suite |
| Frontend unit tests | **22 passed** |
| Playwright E2E | **14 passed** across desktop Chrome and iPhone-sized Chromium |
| Python Ruff lint and format check | **Passed** |
| Frontend format, ESLint/TypeScript, generated API types, production build | **Passed** |
| npm dependency audit | **0 vulnerabilities** |
| Python dependency audit (`pip-audit backend`) | **Passed; no known vulnerabilities** |
| `git diff --check`, changed-file secret scan | **Passed; no whitespace errors or likely secrets found** |
| Source/reference integrity | **Passed; four downloaded originals and six JPEG hashes remain unchanged** |

The browser flows cover successful simulated distribution plus manual completion/reopen, partial failure with destination-only retry and unchanged sibling attempts, UNKNOWN reconciliation without blind retry, exact-version successor safety, material Event invalidation, and the pre-existing campaign, Event-intake, authentication, and responsive-shell regressions.

### 22–26. Security, files, dependencies, conflicts, deferred work

No runtime or frontend dependency was introduced. Simulation outcome injection is available only through an authenticated test-only route mounted only in `environment=test`. API routes remain session-protected. Browser-provided input cannot switch runtime mode, select an unapproved version, widen the stored plan, or authorize execution. Manual Facebook Groups cannot call AUTO execution. No destination credentials were read or added. Source materials remain private through the existing authenticated API; supplied Event Marketing reference originals remain unchanged.

The only prompt/architecture discrepancy concerned LinkedIn personal: the reviewed capability matrix is authoritative, so Event Marketing's access remains unverified and the content is excluded from execution. This implementation does not alter architecture decisions.

Real destination publishing/OAuth, real OpenAI marketing generation, image generation, Facebook Group ranking/selection, real account readiness checks, and live Google OIDC verification remain deferred. WP1E uses only deterministic simulated adapters. Live Google sign-in remains pending deliberately provisioned provider credentials.

## Files changed

- Backend: `app/campaign_service.py`, `app/event_routes.py`, `app/execution_routes.py`, `app/execution_service.py`, `app/main.py`, `app/models.py`, `app/routes.py`, `app/simulated_connectors.py`, `migrations/versions/0004_approval_and_execution.py`.
- Backend tests: `tests/test_execution.py`, `tests/test_foundation.py`.
- Frontend: `src/App.module.css`, `src/api.generated.ts`, `src/campaigns.tsx`, `src/campaigns.test.tsx`, `e2e/campaign-execution.spec.ts`.
- Documentation: this report, `docs/approval-and-simulated-execution.md`, and README status text.
