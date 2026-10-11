# Case study: building an approval-safe AI marketing platform

This is a retrospective on designing and building the platform over an intensive five-day development window. It covers the problem, the decisions that shaped the architecture, the defects that only showed up under integration, and what I would carry into future projects.

## The problem

A small business-networking organization promoted its events by hand. Each event needed copy and a graphic adapted for Eventbrite, Meetup, LinkedIn, Instagram, and dozens of community Facebook Groups. The goal was to cut that work down to *supply the details → review → approve*, without ever letting an AI model, a stale configuration, or a retried request publish something nobody approved.

Two constraints drove almost every design decision:

1. **AI output is never authority.** Models may propose facts, copy, visuals and rankings. Only deterministic services can accept facts, change state, or publish.
2. **External effects are not transactional.** A provider call can succeed while the local commit fails, or time out after it already created a post. The system has to stay honest about what it does not know.

## How the work was sequenced

| Phase | Focus |
| --- | --- |
| Evidence audit | Inventoried the supplied material before modeling anything (see lesson 1). |
| Architecture review | Wrote boundaries, the domain model and the state machine, then corrected them before writing code: approval was separated from execution start, and one-to-one Event↔Campaign cardinality was dropped. |
| Protected foundation | Google OIDC login, database-backed sessions, CSRF, and the browser shell, before any business workflow. |
| Intake and extraction | Canonical facts kept separate from extraction *proposals*. |
| Versions and simulated execution | Immutable campaign history and exact-version approval came before any real publisher. |
| AI copy and graphics | Replaceable adapters plugged into the existing domain boundaries. |
| Live adapters | Eventbrite, Meetup, LinkedIn Organization and Instagram, each with an explicit supported subset. |
| Security review and integration audit | A dedicated audit pass found cross-component defects that component tests had missed, and they were fixed. |
| Pilot hardening | Forgiving schedule input, description suggestions, AI factual-claim reliability. |

## Lessons worth keeping

### 1. Verify the dataset before you model it
The project handoff described a historical "events master" spreadsheet with a specific row and column count. An audit found no such dataset could be verified. Designing a schema around it would have created fake fields and false constraints. The same audit caught a document-reading library that *fabricated* metadata timestamps when the originals were absent. The fix was to read the raw OOXML and keep absence as `null`.

**Principle:** establish that a dataset exists, who owns it and what it means before you derive architecture from it. Tools can manufacture authoritative-looking evidence.

### 2. Approval must match what the user actually reviewed
An integration audit found that the server rebuilt the destination plan at approval time without checking it against the preview the user had seen. Storage was immutable and internally consistent, but a configuration change between preview and click could still produce an approval of a different plan. The fix is a compare-only `expected_destination_plan_hash`: the server recomputes under lock and rejects any mismatch with HTTP 409, creating no Approval row.

**Principle:** executing an unreviewed plan correctly is still an authorization defect.

### 3. Hash content and execution intent separately
The first design stored one global runtime mode per approval. That could not express "Eventbrite LIVE, Meetup SIMULATED, Facebook Groups MANUAL" for the same campaign. I replaced it with a content hash plus an independent, immutable destination-plan hash. A migration preserved the meaning of historical approvals and refused downgrades it could not represent.

**Principle:** freeze execution-significant configuration inside the authorization object, so a later settings change can never upgrade old consent into live authority.

### 4. Persist authorization before fallible execution
Approval commits in its own transaction, and execution initialization consumes it in a second one. If startup fails, the user can resume without regenerating content or approving again.

### 5. Uncertainty is an outcome, not an error to retry
If a provider might have created a resource, the result is `UNKNOWN`. Known resource IDs are bound *before* readback, so reconciliation can query the exact resource instead of fuzzy-searching by title. The audit also found that an interrupted attempt could wedge a campaign forever. Durable leases fixed that: an expired publish lease recovers to `UNKNOWN`, never to "retry". It is safe, recoverable, and honestly not exactly-once.

### 6. Separate static capability from mutable readiness
Connecting an OAuth account in Settings didn't make a destination approvable until the server restarted, because readiness was captured when the adapter registry was built. The test harness hid this: its fixtures rebuilt the registry and supplied fallback credentials. Readiness is now evaluated dynamically behind static adapter registration, and a regression test connects and disconnects with no restart.

### 7. Test fixtures must not repair the behavior under test
The defects in lessons 2, 5 and 6 all shipped through hundreds of passing component tests. Fixtures that "helpfully" set up state production never sets up, and assertions that stopped at `BLOCKED` without proving the user could resolve it, created false confidence. The fix was integrated route-level tests that fake only the *external* HTTP boundary and follow full lifecycles: connect → approve → disconnect → blocked → resolve.

### 8. Let models propose; render authoritative data deterministically
Image models are unreliable typographers. The graphic pipeline asks the model for a visual layer only, then composes the exact title, schedule and venue locally with versioned layouts. Layout-only revisions reuse the visual without another paid image call. When copy needs an exact factual echo for validation, the prompt includes a server-built reference of the canonical facts. That single change fixed a claim-echo mismatch in a smoke test, and the independent validator stayed in place.

### 9. Do slow work outside locks, then commit conditionally
AI generation captures its inputs, calls the model with no locks held, then re-locks and compares facts, revision and version before appending. Stale results are rejected rather than written. The trade-off is that two concurrent first requests can both spend a model call before one wins.

### 10. Credentials: authenticate the context, not just the bytes
Vault envelopes use AES-GCM with provider, connection, schema and key identity in the associated data, so a ciphertext cannot be moved to another record. The security review also found a key-rotation tool that took locks in the opposite order to the runtime (a latent deadlock), and a test mode with a constant vault key that could have been deployed. Both were fixed: rotation now follows the runtime lock hierarchy, and test mode is loopback-only with a random per-process key.

### 11. Treat provider capability as an explicit contract
A shared adapter interface does not make providers interchangeable. Eventbrite blocks paid workflows. Meetup supports only free, in-person events and blocks graphics until binary upload is verified. LinkedIn allows copy-only successor updates. Instagram requires a graphic and blocks automatic successors. Each block happens *before* any mutation.

### 12. Synchronize browser tests on product state, not time
Intermittent Playwright failures came from helpers that raced asynchronous approval controls, and from fake OAuth state leaking between desktop and mobile projects. The fix was waiting on observable readiness and resetting shared fakes. Approval guards were never relaxed to make tests pass.

## Known open issues

- Initialization validates every selected Facebook Group up front, so one unavailable group can block unrelated destinations.
- The Start request dispatches destinations sequentially in-process, and latency under real provider timeouts is unmeasured.
- Architecture docs describe a durable `GENERATING` state. The implementation uses capture → call → conditional commit instead.
- No PWA manifest or service worker yet, despite the original design intent.
- Real-account publishing, production deployment, and backup/restore of the database, private assets and keyring together have not been verified.

## Testing approach

- **Backend:** pytest against disposable PostgreSQL databases created per run, with migrations applied. Stateful HTTP fakes cover Eventbrite, Meetup, LinkedIn, Instagram, and OpenAI.
- **Frontend:** Vitest + React Testing Library.
- **End-to-end:** Playwright desktop and mobile Chromium projects against a test-mode backend with an isolated identity provider. There is no login bypass.
- No test ever contacts a real provider or AI API.
