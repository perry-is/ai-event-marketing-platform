# Work Package 1M-R2 report

Status: implementation complete; verification passed. Baseline was clean at `18aeac019addf88cb4d73ce3368955c819781758` (`test: expand system integration audit coverage`). Source-integrity validation passed at baseline and after implementation. No provider accounts were connected and no real provider calls or writes were made.

## Remediations

H1 makes LinkedIn and Instagram account/credential readiness dynamic behind static adapter registration. OAuth connect and disconnect immediately affect Settings and approval preview without restart or registry reconstruction. Existing stored LIVE approvals remain LIVE and block if the current approved connector cannot honor them.

H2 exposes existing allowed acknowledgement for eligible BLOCKED outcomes. Acknowledgement accounts for the outcome without changing BLOCKED into success. Deterministic Instagram already-published and LinkedIn unsupported-media successor blockers appear before approval as unable to publish automatically. Completion still waits for every required target, including manual work.

H3 adds `ExecutionAttempt.lease_expires_at` in migration `0010_execution_attempt_lease`. A 30-minute lease exceeds the current connector request/poll bounds. Expired publish work becomes UNKNOWN with `EXECUTION_INTERRUPTED`; expired reconciliation remains UNKNOWN. Recovery is lazy on relevant reads and commands, transactional/idempotent, preserves attempts, does not retry, and leaves unexpired work untouched across worker startup.

M1 binds approval to the exact preview by requiring `expected_destination_plan_hash`. The server recomputes the plan and returns `APPROVAL_PLAN_CHANGED`/409 on drift without creating an Approval. The UI refreshes and waits for a new explicit approval. Preview target labels are safe server-derived presentation; stable identities, not cosmetic labels, determine the hash.

L10 carries runtime mode into replacement confirmation so LIVE Meetup cannot be mislabelled as simulated. L11 waits on actual replacement UI readiness and checkboxes rather than sleeps. LinkedIn OAuth test state is reset before connector browser scenarios to keep desktop/mobile execution order independent.

## Data, API, and migration

One migration adds a lease-expiration timestamp to `execution_attempts`, backfills existing RUNNING attempts, and adds an index for expired lease lookup. No other schema change was introduced. Approval API now requires a 64-character expected destination-plan hash. Generated API types were refreshed; the OpenAPI contract change does not add browser authority over plan contents or modes.

## Validation

Fresh PostgreSQL backend run: **285 passed, 0 failed, 0 errors, 0 xfailed**. Frontend unit tests: **57 passed**. Frontend lint/type check, Prettier, production build, `npm audit` (0 vulnerabilities), `pip-audit` (no known vulnerabilities), Ruff lint/format, configured-database migration to head, and `alembic check` passed. The backend migration test exercised empty-database full upgrade, repeat upgrade, downgrade, and re-upgrade. Full desktop/mobile Playwright passed **58/58 twice consecutively**. Final `git diff --check`, secret scan, and source-integrity validation passed; no source/reference files changed.

The initial acceptance pass exposed an Instagram E2E that had not accounted for an outstanding Facebook manual action and mobile test state leaking between OAuth and LinkedIn connector scenarios. Frontend lint also flagged an unnecessary mutable declaration and unused test variable. The E2E now accounts for the remaining manual action before asserting campaign completion, resets fake LinkedIn OAuth state before connector scenarios, and uses lint-clean test declarations. Focused mobile Instagram/LinkedIn flows passed 7/7, followed by two complete 58/58 desktop/mobile passes with no skips or failures.

## Deferred findings and limitations

M2 (synchronous Start request architecture), L4 (unavailable Facebook Group isolation), L6 (inferred identical-plan reapproval uniqueness edge case), and unrelated low-severity audit findings remain deferred. Based on the remediation-specific evidence, the audit recommendation can move from C to **B — Integration sound with minor fixes**, subject to Architect review. This records integration correctness evidence only; it does not declare production readiness or completion of the separate production-hardening phase.
