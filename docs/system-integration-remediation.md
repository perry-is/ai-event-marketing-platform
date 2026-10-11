# System integration remediation

This note records the focused fixes made for WP1M-R2 after the independent audit in [system-integration-audit.md](system-integration-audit.md). The historical findings remain in that report; this document records how the confirmed blockers were addressed and what remains deferred.

## H1 — dynamic LinkedIn and Instagram readiness

The registry previously treated credential/account readiness as a construction-time fact. A successful OAuth connect could update Settings while the existing adapter declaration still blocked approval until restart. Registration is now static, while LinkedIn and Instagram live adapters resolve account and credential readiness through their current provider-connection/vault boundary whenever readiness is evaluated. Settings and approval use the same persisted connection source. Connect or disconnect is visible immediately; a stored LIVE approval remains LIVE and blocks if its approved capability is unavailable. No registry rebuild or restart is used.

## H2 — resolving BLOCKED outcomes

The distribution panel previously rendered resolution controls only for FAILED outcomes. It now presents the safe, policy-approved “Account for this” action for eligible BLOCKED results and refreshes distribution after acknowledgement. Acknowledgement preserves BLOCKED history and does not claim success. Completion remains derived from every destination in the exact approved plan, so unrelated manual work must also be completed or accounted for. Known immutable successor blockers for Instagram and LinkedIn are surfaced in approval preview as unable to publish automatically, with their human-readable reason; they are not presented as live-ready. Runtime-only failures remain at execution preflight.

## H3 — durable execution leases

Publish and reconciliation attempts now carry a durable `lease_expires_at`. The default lease is 30 minutes, longer than the bounded request and processing-poll windows in the current Eventbrite, Meetup, LinkedIn, and Instagram adapters (the longest current Instagram processing wait is under five minutes). Recovery runs lazily on distribution/status access and relevant Start, retry, reconcile, and disposition operations. It uses the existing Event → Campaign → execution lock order and rechecks state/expiry while locked. An expired PUBLISH becomes UNKNOWN with `EXECUTION_INTERRUPTED`; an expired RECONCILE attempt remains UNKNOWN and is finalized as inconclusive. Attempts and evidence are retained, and no publish is retried. Unexpired leases are never expired merely because another worker starts. The migration backfills prior RUNNING attempts conservatively and adds a partial index for lease recovery.

## M1 — approve the preview the user reviewed

Approval preview returns the deterministic destination-plan hash and safe display labels for frozen targets. The browser submits only `expected_destination_plan_hash`, not the plan or execution modes. During approval, the server rebuilds the plan under the existing transaction/locks and compares its hash. A mismatch returns `APPROVAL_PLAN_CHANGED` with HTTP 409 and creates no Approval; the UI refreshes the preview and requires another explicit approval. Cosmetic target labels and transient readiness remain outside hash authority; stable target identities remain inside it.

## L10 — Meetup replacement label

Replacement confirmation now carries each prior destination's runtime mode through the UI formatter. A verified LIVE Meetup execution is labelled Live; SIMULATED executions remain Practice mode. Execution behavior is unchanged.

## L11 — Meetup Playwright synchronization

The test helper now waits for the replacement prompt or an enabled approval action, then waits for the actual acknowledgement checkbox before checking it. It waits for approval readiness rather than relying on sleeps or weakening the product guard. The LinkedIn browser fixture also resets its OAuth connection between project runs, preventing desktop/mobile test order from leaking a prior fake account into readiness checks.

## Deferred audit findings

This package does not implement the synchronous Start-request architecture improvement (M2), unavailable Facebook Group isolation (L4), inferred identical-plan reapproval uniqueness edge case (L6), general low-severity cleanup, or unrelated test-result normalization. These remain follow-up work. The audit's verified authorization, exact-version, target-freezing, retry, UNKNOWN, manual Facebook, credential-vault, and mixed-mode safety invariants were preserved. No real provider account or write was used.
