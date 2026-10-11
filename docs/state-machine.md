# Application lifecycle contract

WP0B proposal with WP0C boundary corrections. Lifecycle describes campaign work; destination outcomes are separate. Clarification is a DRAFT subactivity driven by deterministic canonical/destination missing/invalid fact checks and unconfirmed ambiguities, not an additional persisted state. A model can propose content/fact edits or questions, but cannot authorize transitions.

## Campaign states

| From → to | Trigger / prerequisites | Authority |
| --- | --- | --- |
| New → DRAFT | User starts intake; backend creates Event/Campaign. Partial information allowed. | Authenticated user command. |
| DRAFT → GENERATING | Enough accepted facts for a useful preview; confirmed input revision and one generation token. Approval readiness may still require clarification. | User request; deterministic backend guard. |
| GENERATING → REVIEW | Complete snapshot/assets/plans validate; input/token/revisions unchanged. | Backend, not model response alone. |
| GENERATING → DRAFT | Generation fails, refuses, times out, process dies or becomes stale; keep originals and previous snapshots. Show actionable failure; retry captures fresh input. | Backend. No execution/approval created. |
| GENERATING → DRAFT | User changes accepted facts/revision request; invalidate generation token before changing input. Discard any later completion. | User/backend. |
| REVIEW → DRAFT | User requests revision/accepts fact change. Old snapshot preserved; active authorization absent. | Backend accepts user request. |
| REVIEW → APPROVED | Exact approval transaction in campaign-versioning.md passes. | Explicit authenticated user approval. |
| APPROVED → EXECUTING | At least one authorized unit is claimed or manual package delivered; separate start action initializes units; dispatch checks current approval/material intent/kind/readiness/mode/account. Failure to start leaves APPROVED resumable. | Backend after approval, no second marketing decision. |
| APPROVED or EXECUTING → COMPLETE | Every selected unit has an explicit disposition under completion rules below. | Deterministic backend; user must acknowledge unresolved known failures/blocks when closing them. |
| APPROVED/COMPLETE/EXECUTING → DRAFT | Material revision, only when no affected RUNNING or unresolved UNKNOWN operation; withdraw current authority and supersede undispatched work. | User request, backend transaction. |
| COMPLETE → EXECUTING | User retries a specific FAILED/BLOCKED unit; original approval still active, retry safe, dependencies/current account authorized. | Backend after explicit user retry. No successful unit republished. |
| DRAFT/REVIEW/APPROVED/COMPLETE → ABANDONED | User abandons; withdraw approval, explicitly skip pending work, retain history. | User. |
| GENERATING → ABANDONED | User cancels; invalidate generation token; late result discarded. | User/backend. |
| EXECUTING → ABANDONED | Record cancel_requested_at, clear active approval immediately and stop/skip undispatched work; only enter ABANDONED after in-flight operations settle and UNKNOWN outcomes reconcile or are explicitly abandoned with risk acknowledgement. Until then show cancellation requested while EXECUTING. Already-sent results remain collectable without active approval. | User/backend. |

ABANDONED is terminal; starting again can create a replacement Campaign for the same Event if desired, without deleting history. Intake, source proposals and group decisions may update within DRAFT under revision guards; result/disposition updates can occur within EXECUTING/COMPLETE without inventing another lifecycle state. No automatic schedule recurrence or unattended future publishing in Phase 1.

## Destination execution statuses

| Status | Meaning / legal follow-up |
| --- | --- |
| READY | Authorized unit exists, not sent. Can be claimed RUNNING, become BLOCKED by account/dependency, or be explicitly SKIPPED. |
| RUNNING | One leased attempt underway. Finalize as SUCCEEDED, FAILED, UNKNOWN or MANUAL_PENDING. A manual acknowledgement may finalize MANUAL_DONE directly. An expired active lease is atomically recovered to UNKNOWN (or leaves a reconciliation attempt unresolved against the UNKNOWN execution); it is never reset to READY or retryable automatically. |
| SUCCEEDED | Required creation/verification evidence met. Terminal for publishing; no retry publish. Simulation is displayed “Simulated success,” never a live publication. |
| FAILED | Known non-success, with confirmed remote outcome/error classification. Retry only when safe and authorized; closing as failed records user acknowledgement. |
| UNKNOWN | Creation might have occurred. VERIFY can establish qualifying success or confirmed absence; explicit attestation of a found matching post can establish MANUAL_DONE/HUMAN_ATTESTED. Never blindly publish again or silently close as success/failure/COMPLETE. |
| MANUAL_PENDING | Exact approved package delivered for the target; external posting is still unconfirmed. User may acknowledge MANUAL_DONE with optional returned link, explicitly accept handoff responsibility for the finished package, or SKIP. Delivery alone does not close a required manual task. |
| MANUAL_DONE | User-attested manual action or reconciliation of a found AUTO effect; distinguish from machine-verified SUCCEEDED. No automatic verification or safe-absence claim. |
| BLOCKED | Authorization, dependency, account access or capability prevents current action; no request sent. Resolve to READY under the same approved intent when possible, or use the existing policy-approved acknowledgement/skip action. Acknowledgement accounts for the outcome without converting it to success. |
| SKIPPED | Explicitly declined/superseded, with actor/reason. Terminal; another publish intent requires a new version. |

Failure of one destination does not rollback success elsewhere. Dependencies, if declared, block only dependent targets. Pending manual groups do not keep unrelated publishers waiting. One unresolved target is visible alongside completed work.

## Completion semantics

COMPLETE means **“The campaign execution workflow has no unresolved required action.”** It does not mean every destination succeeded. Derive completion from the exact approved included target set, not just whichever execution rows happen to exist: an APPROVED version with no started units is not COMPLETE.

Eligible dispositions: SUCCEEDED, MANUAL_DONE, SKIPPED; MANUAL_PENDING only with an explicit user-accepted MANUAL_HANDOFF of its available exact package; FAILED/BLOCKED only with user acknowledgement bound to the current result fingerprint/attempt. Mere package delivery does not resolve a required human task. READY/RUNNING and unresolved UNKNOWN do not qualify. An UNKNOWN may be accounted for only by qualifying reconciliation or explicit ABANDONED_UNCERTAIN risk acknowledgement under [execution safety](execution-safety.md#explicitly-accounting-for-an-uncertain-outcome); retain uncertainty and prohibit publish retry. No automatic closure timer. At least one included AUTO/MANUAL target is required for approval.

Outcome/health is a derived summary, not more top-level states. Show counts and all applicable labels:

| Derived outcome | Meaning |
| --- | --- |
| Completed successfully | All included targets have qualifying success evidence; simulated results visibly labeled. |
| Completed with manual actions | Human-attested completions or accepted package handoffs exist. Show external manual work outstanding for handoffs; do not call delivery publication. |
| Completed with acknowledged failures | Known failed/blocked/skipped work has explicit disposition; successes stay visible separately. |
| Unresolved UNKNOWN outcome | Required verification/reconciliation action remains; campaign cannot be COMPLETE. |
| Completed with acknowledged uncertainty | User explicitly abandoned uncertain work; possible remote effects remain prominently visible and never count as success/absence. |

Example: “Complete: 2 simulated successes, 3 manual packages handed off, 1 failed and acknowledged.” Later manual acknowledgement updates history without reopening; safe failed-target retry clears its prior disposition and reopens EXECUTING. Attempt evidence and disposition remain separate. Cancellation/abandonment uses the same accounting guards; in-flight results remain collectable after authority is withdrawn.

UI progress is derived from stored state, never from an AI statement or a successful HTTP submission. Requests use optimistic revision tokens; stale browser commands fail with a refreshable conflict, not a second hidden operation.
