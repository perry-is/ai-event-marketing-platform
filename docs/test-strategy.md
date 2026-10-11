# Tests focused on business invariants

WP0B proposal with WP0C acceptance corrections. No application/tests are implemented in this package. Phase 1 tests prove actual protection at the command/persistence boundary with sanitized fixtures and simulated publishers, not just presentation or coverage percentage. Minimum acceptance suite follows the same invariants real adapters must later satisfy.

## Required invariant cases

| Invariant | Meaningful test / primary level |
| --- | --- |
| Unapproved campaign cannot execute | Call dispatch directly without approval/active pointer, including forged browser/model request; assert no attempt/adapter call. Integration. |
| Approved snapshot immutable | Attempt content UPDATE/DELETE through app service and runtime DB role; reject, hash/manifest/bytes unchanged. PostgreSQL integration. |
| Material change requires successor approval | Material fact edit or “warmer LinkedIn” revision withdraws active pointer, creates new immutable version on successful assembly; old approval cannot execute new work. State/integration. |
| Approval survives execution-start failure | Fail transaction B or stop after approval transaction A; approval/history stays committed, no execution falsely claimed; resume initializes exact units once with no reapproval. Integration/E2E. |
| Event revision is provenance, not blanket invalidation | Change internal note and increment revision after approval; original version remains executable. Change date/location/price used by an approved plan; all affected campaigns lose authority even if execution rows do not yet exist. Preserve old Approval/history; successor needs fresh approval. Integration/state. |
| Conditional requirements are deterministic | In-person requires usable location; virtual requires attendance/registration strategy; paid listing requires plan ticket settings that social-only campaign does not. Fake AI requests arbitrary mandatory facts; code ignores that demand. Unit/E2E. |
| Preapproval target validation | Missing required selected-target fact blocks approval; explicit exclusion leaves no execution unit. Simulated READY is not LIVE readiness; personal LinkedIn conceptual AUTO is preserved even while access is UNVERIFIED. Integration/contract. |
| Post-approval transport is mechanical | Exact rendered copy/assets survive field/enumeration/timestamp mapping; adapter cannot truncate/rewrite/choose hashtags/substitute media. Changed meaningful constraint blocks for new review, never silently repairs. Contract. |
| Event owns many Campaigns | Database permits a second Campaign for an Event; V1 service returns existing active primary under concurrent creation. No unnecessary uniqueness or future campaign features. Integration. |
| Connector cannot execute another version | Swap version/hash/target/mode in execution context, including a valid approval from another Campaign; reject before effect. Contract/integration. |
| Independent retry | Instagram simulated success, LinkedIn confirmed failure, retry LinkedIn; assert one Instagram publish and two LinkedIn attempts sharing logical key. Integration/E2E. |
| Manual target cannot auto-publish | Try AUTO context/config/model selection for Facebook Groups; reject. Package delivery and HUMAN_ATTESTED completion stay separate from auto success. Contract. |
| Unsupported target cannot execute | Select LIVE UNVERIFIED LinkedIn personal/Instagram, unclassified Portal or excluded Facebook Page; approval validation fails, no adapter call. Capability changes after approval block rather than substitute mode. Contract/integration. |
| AI cannot mutate protected state | Malicious schema-valid proposal demands approval/state/credentials; reject fields, no state transition/authorization. Unit/integration. |
| Accepted facts cannot be overwritten by extraction | New source contradicts confirmed date/title; stage conflict, preserve existing revision until explicit user acceptance. Integration/E2E. |
| Source is not runtime truth | Modify an extracted proposal/replace supplied input copy; immutable original hash and approved facts remain unchanged, no re-read live file sync. Integration. |
| Success needs evidence | HTTP accepted/empty id or wrong-target URL/refused verification cannot set SUCCEEDED. UNKNOWN has no publish retry. Contract. |
| Authorization required | Anonymous/expired/disabled user and CSRF failures rejected for intake/read/files/approval/retry/manual acknowledgement. No leaked private content. Integration/E2E. |
| Duplicate/concurrent submits safe | Two approving clients, separate duplicate execution starts and simultaneous retry calls on separate DB connections result in one approval/unit/active publish attempt. Stale revision returns conflict; deadlock-safe consistent lock order. PostgreSQL integration. |
| Timeout after creation safe | Fake remote creates then times out; execution UNKNOWN. Verify matching evidence records result without a second publish. Inconclusive verify stays UNKNOWN; confirmed absence alone permits safe retry. Contract/integration. |
| Crash/lease/stale completion | Crash after claim/possible dispatch, restart and expired lease; never automatically creates again. Late generation token cannot replace newer draft; late network result cannot clobber newer reconciliation. Integration. |
| Supersession, cancellation and prior effects | Reject material edit while affected RUNNING/unresolved UNKNOWN; safe material edit withdraws authorization and skips old pending units. Cancellation clears active approval/stops new claims immediately but permits evidence-only settlement/reconciliation of sent attempts. Prior success/package delivery visible; successor requires explicit replacement intent before targeting it again. State/E2E. |
| Completion not false success | Package available alone cannot resolve required manual work; explicit handoff + acknowledged failed target may reach COMPLETE with visible summary. Missing units/READY/RUNNING/unresolved UNKNOWN cannot close; explicit uncertain abandonment keeps remote uncertainty and prohibits retry. Later manual acknowledgement keeps COMPLETE; failed-target safe retry reopens. State/E2E. |
| Version/asset and binding integrity | Hash changes if copy/targets/assets/mode change. Missing/replaced asset blocks approval. Only declared registration slot accepts verified producer URL; consumer retry uses same pinned binding. Integration/contract. |
| Untrusted upload | Wrong MIME/extension, large decoded image, encrypted/malformed PDF, ZIP expansion, path traversal, script/HTML rejected. Originals/derivatives private; model prompt injection cannot fetch URLs or act. Unit/integration. |

## Test boundaries

- **UNIT:** pure factual/schema/readiness checks, material dependency comparison, canonical serialization, capability constraints, URL/key handling, completion reducer, proposal validation. Test business input variations, not duplicate implementation assertions.
- **INTEGRATION:** FastAPI command/auth/session behavior with real PostgreSQL and private temporary storage; transaction rollback/ownership, runtime-role immutability, constraints, concurrent connections, audit atomicity. SQLite cannot substitute for locking/JSON/trigger acceptance tests.
- **STATE-MACHINE:** table-driven allowed/forbidden transitions and concrete multi-step histories, including partial results, cancellation, stale commands and reapproval. Test actual domain service, not a separate test-only state machine.
- **CONTRACT:** same adapter tests for each fake AUTO/MANUAL implementation and UNVERIFIED/BLOCKED readiness fixture; input identity, render determinism, normalized errors/evidence, no post-approval marketing changes. Future live adapters additionally need provider-specific account/absence/creation evidence tests before activation.
- **END-TO-END:** a few Playwright paths: intake → confirm → clarify → review/revise → approve → simulated distribution/history; one fail/retry; manual groups; stale approval; unauthenticated/file boundary. Assert user-visible semantics plus durable results, not pixel-perfect design.

Fake provider test harness is independent of app's success assignment, can hold simulated remote receipts across a backend restart, and can script failure before/after remote creation. Never use a real account/live source to prove these cases. Fake model supplies deterministic useful proposals and adversarial/refusal outputs. Freeze clock and request identifiers where needed; don't assert transient provider prose.

Phase 1 completion requires this core invariant suite and one authenticated clickthrough. Real OIDC login smoke test only when operator supplies configured identity client; provider-mocked tests still validate callback/session guards. No production auth bypass passes acceptance. Production publishing tests are outside Phase 1.

Documentation-package validation: current HEAD/status, complete diff review, `git diff --check`, local Markdown links, targeted secret scan, existing read-only `scripts/verify-source-audit.py`, and confirmation backend/frontend remain `.gitkeep` only. These validate the documentation package integrity, not an unimplemented application's correctness.
