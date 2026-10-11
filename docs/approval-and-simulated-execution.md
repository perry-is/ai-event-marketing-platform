# Approval and simulated execution

WP1E established the approval and execution boundary. WP1I-A corrects it to support a server-derived mixed plan under one approval. Every route requires the authenticated application session and uses the existing same-origin/CSRF checks for mutations. The current destination defaults remain simulated, except Facebook Groups is manual. No real live destination adapter is implemented; only a test-only Eventbrite LIVE-shaped adapter exercises dispatch without network access.

## Approval

Approval authorizes one immutable CampaignVersion and one exact immutable destination plan shown during review. The persisted Approval records the Campaign and CampaignVersion IDs, CampaignVersion content hash, destination-plan snapshot and independent hash, selected targets and exclusions, source Event revision, approving application user, and timestamp. New approvals do not store a global runtime mode; nullable legacy `runtime_mode` and `approved_plan_hash` fields are historical metadata only. A PostgreSQL trigger rejects Approval updates and deletes. The Campaign's `active_approval_id` points to current authority; the latest version number is never used as authorization.

Approval and execution are separate backend transactions. The UI first saves approval, then exposes a start action labeled from the stored plan. If execution initialization fails, the saved Approval remains active and can be resumed without another approval. Starting or retrying always reloads and verifies the exact approved version/content hash, stored destination-plan hash, active approval, and current material Event facts. Execution uses the stored plan; it never rebuilds modes from current settings.

Approved CampaignVersion content is never edited. A copy revision creates a successor version and withdraws the earlier active authorization while preserving its Approval and execution history. A material Event fact change also withdraws active authority, preserves the prior Approval and version, and prevents new execution of the obsolete version. The Event material projection decides invalidation; an administrative/internal-note change alone does not invalidate approval. In-progress or unresolved UNKNOWN execution blocks a material edit or successor revision until the result is accounted for.

## Destination plan and capability boundary

The destination plan is constructed and validated before approval, then stored inside the immutable Approval. The server derives AUTO destination modes from per-destination settings. The browser submits only the preview's expected plan hash as compare-only intent; it cannot submit plan contents or a mode. If the authoritative plan hash changes before approval, the server returns a conflict and requires a refreshed review. The plan hash covers execution-significant destination/target values, AUTO mode, and manual target set, but excludes transient readiness, cosmetic labels and timestamps. Config changes may make an approved entry unavailable; they cannot change its mode. A deterministically known accountable successor limitation is shown before approval as blocked from automatic publishing, not as publishable. Eventbrite, Meetup, LinkedIn organization, and Instagram currently default to AUTO/SIMULATED. Facebook Groups produce one MANUAL package containing the exact selected Group set and never call an AUTO execution method. Facebook Page is excluded.

LinkedIn personal copy may remain visible in the CampaignVersion, but current Event Marketing access and routing are unverified in the reviewed capability matrix. It is explicitly marked content-only and excluded from execution, not represented as an executable target. This preserves the reviewed architecture boundary while retaining the copy for the operator's review.

Every simulated adapter declares destination, execution kind, simulated/live readiness, adapter revision, and verification support. The current adapter implementation validates its plan entry, performs only mechanical field mapping, executes a deterministic outcome, verifies simulated receipts, and returns a normalized result. No connector has platform credentials, OAuth, or a network publishing client. Success requires a simulated identifier, a `simulated.invalid` URL, and explicit receipt evidence.

## Identity, attempts, retries, and reconciliation

Each execution row is bound to one Approval, its CampaignVersion, destination, target, approved treatment and per-entry runtime mode (null for MANUAL), plus the destination-plan hash. A stable SHA-256 idempotency key and database uniqueness constraint prevent duplicate units within an approval. A destination retry acts on exactly one existing execution row and creates a new numbered attempt under that identity. It cannot re-run successful sibling destinations. Retries preserve the mode stored in the approved plan even if current settings change. Successful, running, UNKNOWN, manual, and non-retryable rows reject blind retry. A partial unique index prevents more than one running attempt for the same execution.

ExecutionAttempt rows preserve publish, verification, manual-package, manual-attestation, reconciliation, and disposition history. Finalized attempts and AuditEvents are append-only at the PostgreSQL boundary. An adapter timeout or ambiguous result becomes UNKNOWN. UNKNOWN cannot be retried until verification determines a matching result or confirms absence; an inconclusive result remains unresolved. An authorized human may explicitly acknowledge uncertainty, which remains visibly labeled as acknowledged uncertainty rather than success.

The test-only scenario control injects deterministic SUCCESS, retryable failure, final failure, or UNKNOWN results and reconciliation findings. It is mounted only when the application environment is explicitly `test`; production configuration has no scenario route or controller.

## Manual destination and completion

The Facebook Groups package snapshots the approved copy, Event title, group-selection reminder, creative direction/status, and manual posting instructions. It includes a copy-text control. The user marks the single package complete after posting outside the application; no per-Group checklist or automatic posting is implemented. Manual completion records the application user and time as an execution attempt and audit action.

`COMPLETE` means no required execution action remains unresolved. It does not imply all targets succeeded. The derived outcome distinguishes successful completion, manual completion, acknowledged failures, and acknowledged uncertainty. Unresolved UNKNOWN remains incomplete. An append-only campaign-completion audit row records when the workflow first reaches a complete outcome for an Approval; that timestamp is included in the distribution summary and UI.

Final failures and eligible BLOCKED outcomes require explicit acknowledgement before they can be accounted for. The UI exposes the existing acknowledgement route only where backend policy allows it; acknowledgement does not convert BLOCKED or FAILED into success. Manual work can be completed, handed off with posting still unverified, or explicitly skipped where permitted. Each choice has a distinct status/disposition and is preserved in history.

## API surface

- `GET /api/campaigns/{campaign_id}/approval-preview` validates and summarizes the current version's exact plan.
- `POST /api/campaigns/{campaign_id}/approve` approves the supplied current version ID/hash only when `expected_destination_plan_hash` still matches the server-rebuilt plan.
- `POST /api/approvals/{approval_id}/start` starts only that saved authorization.
- `GET /api/campaigns/{campaign_id}/distribution` returns approval/version history, destination attempts, derived outcome, and completion time.
- Destination-execution commands provide retry, reconciliation, manual completion/handoff, failure or uncertainty acknowledgement, and explicit skip.

Hashes and identifiers are integrity/provenance data, not credentials. API responses contain no provider tokens or live platform payloads. Source-material storage remains behind the existing authenticated Event download boundary.
