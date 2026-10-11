# Full system integration audit: AI Event Marketing Platform

An independent end-to-end audit of whether the implemented components form one coherent, correctly wired application. Passing tests were treated as evidence, not proof. No real provider accounts were connected, no real content was published, and no LIVE credentials were changed. Application code was not modified. The audit added only tests that reproduce or demonstrate findings.

## 1. Baseline

| Item | Observed |
|---|---|
| HEAD at start | `0e3239ce1675ab822f6281cbfe28a7a4ed708926` — `fix: harden credential vault and OAuth security` |
| Working tree at start | Clean |
| Pushed | No |
| Source integrity | PASS (`scripts/verify-source-audit.py`: four downloaded sources and six JPEG hashes unchanged) |
| Migration history | Linear, `<base> → 0001 … 0009_credential_vault (head)`, single head |
| Backend tests at baseline | 279 passed |
| Frontend unit tests at baseline | 55 passed |
| Playwright at baseline | Not rerun before audit changes; see §33 for the final run |

## 2. Repository state

- **`backend/app`:** 31 modules, about 20k lines across the backend and frontend. Key modules:
  - `execution_service.py` (2,236 lines)
  - `instagram.py`, `linkedin.py`, `meetup.py`, `eventbrite.py`
  - `campaign_service.py`, `event_service.py`, `oauth_connections.py`, `credential_vault.py`
- **`backend/migrations/versions`:** 0001–0009.
- **`backend/tests`:** pytest suites over a disposable PostgreSQL database, plus provider fakes:
  - `eventbrite_fake`, `meetup_fake`, `linkedin_fake`, `instagram_fake`
  - `browser_server.py`, a loopback-only Playwright harness
- **`frontend/src`:** React screens and shared modules:
  - Screens: `events.tsx`, `campaigns.tsx`, `facebookGroups.tsx`, `settings.tsx`
  - `campaignGraphic.tsx`
  - `api.generated.ts`, generated from OpenAPI
- **`frontend/e2e`:** 12 Playwright specs, run in desktop and iPhone projects.
- **Source material:** Supplied source originals are read-only inputs. They are verified by hash and never written at runtime (§27, §32).

## 3. Architecture map (from code)

```
Browser (React)           Frontend client            FastAPI route                         Service / domain                              Persistence / provider
New Event (events.tsx)  → api() fetch + CSRF hdr  → POST /api/events, /sources            → event_service + extraction (fake|OpenAI)    → events, source_materials, private storage
Questions               →                         → POST /accept-proposals, /clarifications, PATCH /facts → event_service + invalidate_material_event_approvals → events (+ approvals withdrawn)
Campaign (campaigns.tsx)→                         → POST /events/{id}/campaign, /revisions, /regenerate → campaign_service (+ campaign_ai)     → campaigns, campaign_versions (immutable)
Graphic                 →                         → POST /campaigns/{id}/graphics          → graphics_service (+ image provider)         → assets, campaign_version_assets
Groups (facebookGroups) →                         → /group-recommendations, /group-decisions, /group-selection → group_recommendations (+ ranker) → group_* tables, successor version
Approval                →                         → GET /approval-preview, POST /approve   → execution_service.build_destination_plan    → approvals (plan + hashes)
Distribution            →                         → POST /approvals/{id}/start, /destination-executions/{id}/* → execution_service → DestinationAdapterRegistry
                                                                                          → Simulated | Eventbrite | Meetup | LinkedIn | Instagram | Facebook manual
                                                                                          → destination_executions, execution_attempts, destination_resource_bindings, audit_events
Settings (settings.tsx) →                         → GET /api/settings/*, POST /api/oauth/{p}/connect|disconnect, GET /auth/{p}/callback → oauth_connections + credential_vault → provider_connections, credential_envelopes
Instagram fetch (Meta)  → (public, signed)        → GET/HEAD /api/public/instagram-media/{token} → instagram.signed_media_bytes        → assets (PRIMARY_GRAPHIC of exact version)
```

Composition happens in `main.create_app`:

- Settings, the database, the credential vault and the OAuth service are created first.
- `VaultLinkedInCredentials` and `VaultInstagramCredentials` wrap the OAuth service.
- `CampaignExecutionService` builds the `DestinationAdapterRegistry` once. Each LIVE adapter comes from a factory (`_eventbrite_live`, `_meetup_live`, `_linkedin_live`, `_instagram_live`), alongside a `SimulatedAutoAdapter` per destination and one `FacebookGroupsManualAdapter`.
- At dispatch, adapters are selected by the approved plan entry `(destination_key, execution_kind, runtime_mode)`, never by browser input.

## 4. Workflows traced

The following were traced from browser control to persistence and back. Traces of the same workflows from the earlier WP1M review were not re-derived.

- New Event, intake, extraction, clarification and readiness
- Event to Campaign 1 and successors
- Material Event change
- Facebook Group selection
- Approval construction and the Approval UI
- Execution start, the claim/dispatch/finalize cycle, retry, reconciliation and dispositions
- Every LIVE connector, including vault-backed credential resolution
- Settings, including the OAuth connect/disconnect flow
- Signed Instagram media delivery

The traces combined code reading, two temporary probes (removed) and the committed audit tests (§32).

## 5. End-to-end integration assessment

**The safety core is coherently wired.** These all hold together across the application:

- the exact-version and exact-plan approval
- server-derived per-destination modes
- frozen targets
- exact-graphic delivery
- resource bindings
- UNKNOWN handling
- vault-only delegated credentials

A new end-to-end test proves one comprehensive mocked workflow, with no credential fallbacks, through two simulated process restarts (§32):

- **Event and campaign:** messy intake, clarification, a ready Event, Campaign 1, a graphic, and the human Facebook Group selection frozen into a successor.
- **Credentials:** Settings OAuth into the vault.
- **Approval and execution:** Eventbrite LIVE (real adapter over its fake), Meetup SIMULATED, LinkedIn LIVE and Instagram LIVE (real adapters over fakes, credentials decrypted from the vault), and Facebook MANUAL.
- **Completion:** a manual Facebook completion followed by `COMPLETED_WITH_MANUAL_ACTIONS`.

The workflow around that core has three material defects:

- A Settings OAuth connection does not take effect until a restart (H1).
- `BLOCKED` destinations have no client resolution (H2).
- A process loss during dispatch wedges the campaign and its Event (H3).

All three fail closed: none publishes the wrong content, target or version. They do leave the product unable to finish ordinary work from the UI.

## 6. Event intake assessment

- **Intake and storage:** text and file intake are authenticated and size- and quota-bounded. Files go to private storage via `resolve_storage_path`, which accepts only strict key regexes and stays inside the storage root. Extraction runs after the source commit. Proposals stay pending until accepted.
- **Clarification and edits:** answers, accepted proposals and fact corrections change canonical facts under an Event row lock with an expected-revision check. Each of the three routes calls `invalidate_material_event_approvals` in the same transaction.
- **Readiness:** `event_readiness` gates campaign generation, approval and execution.
- **Source evidence:** it stays traceable (`source_materials`, append-only proposals). Download checks both the Event and the source ID.

No defects found.

## 7. Campaign and versioning assessment

- **Campaign 1:** generation re-locks the Event, re-checks readiness and that the inputs are unchanged, and returns the existing campaign if one was created concurrently.
- **Version numbers:** these are `previous + 1` under the campaign row lock, with a `uq_campaign_version_number` backstop.
- **Successors:** revisions and regenerations re-verify the captured version ID, campaign revision and Event snapshot. They call `prepare_for_successor`, which withdraws the active approval and supersedes undispatched work, and never mutate an earlier version.
- **Graphics on copy-only revisions:** the graphic is inherited (`inherit_primary_graphic`). Regeneration after a material change deliberately drops it.
- **Event binding:** each snapshot records `event_revision` and the full material Event snapshot. Staleness is a projection comparison.
- **Design note:** `_create_version` freezes the latest saved Group decisions into every successor (`assemble_group_targets`). This is by design but worth knowing: any copy or graphic successor also adopts pending Group decisions. Those groups are then visible in the Approval preview.

## 8. Graphics assessment

- **Version binding:** every graphic-bearing connector reads the `PRIMARY_GRAPHIC` manifest row of the exact approved `campaign_version_id`. It checks the asset ID and SHA-256 against the plan entry, and `_read_asset` re-hashes the bytes and checks size, MIME and dimensions.
- **End-to-end proof:** the E2E test confirms Eventbrite and LinkedIn received byte-identical approved graphics, and Instagram received the verified JPEG conversion of that exact asset.
- **Successors:** a graphic change on LinkedIn is blocked before any mutation (`LINKEDIN_MEDIA_REPLACEMENT_UNSUPPORTED`). Instagram blocks any successor. Meetup blocks any graphic.

No wrong-graphic path was found.

## 9. Facebook Groups assessment

- **Recommendations** never write decisions. A `GroupDecision` is created only by an authenticated human decision.
- **Pending changes:** the preview reports `group_selection_pending` until the selection is saved into a successor (proven by the E2E test), and approval rejects with `GROUP_SELECTION_NOT_SAVED`.
- **Frozen selection:** the selected set is part of the version hash (`facebook_group_targets`) and of the plan hash (sorted `selected_groups`).
- **Execution:** one MANUAL entry produces one package. `FacebookGroupsManualAdapter.execute` raises, and there is no per-group execution path anywhere.
- **Catalog drift** after approval blocks delivery (`GROUP_TARGET_UNAVAILABLE`) rather than redirecting. However, this is checked for the whole Start, so one unavailable Group blocks every destination (L4).

## 10. Approval assessment

- **Plan construction:** the plan is built server-side from the CampaignVersion snapshot, the version graphic, and registry modes, accounts and targets. Each target identity is frozen into the entry and its `target_key`:
  - Eventbrite: organization and listing facts
  - Meetup: group, network and member fingerprint
  - LinkedIn: organization URN
  - Instagram: account ID
- **What gets hashed:** the plan hash covers business fields, excluding transient keys. The CampaignVersion hash excludes mode. The browser cannot send a mode: the `ApproveVersion` schema forbids extra fields, and the Playwright mixed-mode spec asserts the body has no `runtime_mode`.
- **Historical approvals** are immutable. Re-approval with the same plan is idempotent.
- **Finding M1:** the approve request does not carry the plan hash shown in the preview. The server rebuilds the plan, so what is persisted is not cryptographically tied to what the operator saw. The UI also never displays the target identity (which Page, account, Group or organization).

## 11. Execution orchestrator assessment

- **Start:** `initialize_execution` locks the Event and Campaign, then asserts active authority:
  - the active approval ID
  - the current version
  - the version hash and plan hash
  - that the version is not stale

  It creates one `DestinationExecution` per stored plan entry, unique on `(approval, destination, target)` plus a unique idempotency key.
- **Claim:** `_claim_auto_attempt` re-asserts authority, selects the adapter from the stored entry, and refuses if the runtime mode differs.
- **Finalize:** results are normalized. LIVE success requires adapter-specific verified evidence. Anything else becomes UNKNOWN.
- **No rebuild from current config:** stored approvals are never rebuilt from current configuration. Readiness re-validation at start can only block.
- **Finding H3:** there is no lease or recovery for an attempt left `RUNNING` by process loss.
- **Finding M3:** Start dispatches every LIVE destination synchronously inside the HTTP request, including bounded provider polling.

## 12. Eventbrite assessment

- **Wiring:** `_eventbrite_live` builds the real `EventbriteLiveAdapter` with `PrivateTokenCredentials` (server token). In tests without an HTTP mock it uses the WP1I-A `TestOnlyLiveModeAdapter`, which cannot be constructed outside test mode.
- **Account binding:** the account (organization, organizer, currency) is frozen and compared on every render (`EVENTBRITE_ACCOUNT_CHANGED`).
- **Create and update:** the first run is CREATE, bound by `(destination, account, event)`. Later runs UPDATE the bound listing. The venue and ticket class are reused through binding metadata, and the logo is uploaded only when the graphic hash changes.
- **Publish flow:** draft-first publish with readback verification. An uncertain create becomes UNKNOWN with the external ID preserved when known. Reconciliation never searches by title.
- **E2E proof:** verified LIVE, one create, an exact graphic, and no duplicate on a second Start.
- Real-account verification remains deferred (§34).

## 13. Meetup assessment

- **Wiring:** `_meetup_live` builds `MeetupJWTTokenProvider`, `MeetupGraphQLClient` and `MeetupLiveAdapter`. The target (network, group, member fingerprint) is frozen.
- **Graphics block early:** `validate` raises `MEETUP_GRAPHIC_UPLOAD_UNVERIFIED` for any plan with an approved graphic. Because readiness calls `validate`, a graphic-bearing campaign is never approvable with Meetup LIVE, and `execute` re-checks before any mutation. No alternate path omits the graphic.
- **Group handling:** a change of group on a bound Event blocks. An approved Group is honored even if the configured Group later changes, provided the account fingerprint still matches.
- **Operational note:** surfaces are fixed at campaign creation. A Meetup-LIVE deployment with any graphic therefore makes those campaigns unapprovable until an operator changes the mode (I2).

## 14. LinkedIn assessment

- **Credentials** come only from `VaultLinkedInCredentials`, which reads the database on every call. The fallback provider is injectable only in test mode, and `create_app` and the service refuse it otherwise.
- **Per-operation checks:** every operation checks scopes, the member's role on the frozen organization URN, and that the frozen organization equals the configured one. Otherwise it blocks with `LINKEDIN_ORGANIZATION_CHANGED`.
- **Content:** Images and Posts APIs with readback. A copy-only successor does a partial `commentary` update of the same post. A graphic successor blocks. There is no delete or repost path.
- **API version** is a validated allowlist sent on every `/rest` call (existing test).
- **Finding H1:** `LinkedInLiveAdapter.__init__` freezes `declaration.readiness_live` using `credentials.configured` at registry construction.

## 15. Instagram assessment

- **Credentials:** Settings OAuth into the vault, then `VaultInstagramCredentials`.
- **Account checks:** the account ID must equal the approved target, the configured ID and the live `/me` response. The account type must be Business or Media_Creator.
- **Signed media:** an HMAC-SHA256 token binds the version, asset, SHA-256, iat/exp (bounded TTL) and a nonce. The endpoint serves only that version's `PRIMARY_GRAPHIC` with matching SHA-256, and returns 404 otherwise. The signing key is separate from the vault. HTTPS and a secret of at least 32 characters are required by both production configuration and readiness.
- **Publish flow:** container, bounded processing polls, `media_publish`, binding before readback, then exact readback and permalink.
- **First publish only:** any existing binding for the Event blocks (`INSTAGRAM_ALREADY_PUBLISHED_IMMUTABLE`) before any provider call.
- **Findings:** H1 applies (`InstagramLiveAdapter.__init__`). H2 applies: readiness does not consult the binding, so the successor preview shows `READY` / "Will publish live" and then blocks at Start.

## 16. Credential-vault integration assessment

- **Runtime wiring:** `create_app` always wires `VaultLinkedInCredentials` and `VaultInstagramCredentials`. There is no environment-token fallback, no direct config token, and no second credential provider.
- **No secrets in config:** `.env.example` holds only app client IDs and secrets plus keyring settings.
- **Disconnect** deletes the envelope in the same transaction, and every `access_grant` re-reads the database, so retrieval stops immediately. This was confirmed in the WP1M review and its existing tests.
- **Restart durability:** credentials survive a process restart. The E2E test uses a shared external key provider, as the production `FileCredentialKeyProvider` would.
- **Test mode** uses a random per-process key, so test-mode envelopes intentionally do not survive a restart.
- **Defect:** the only integration defect is readiness freezing (H1).

## 17. Settings assessment

- **UI wiring:** Settings controls map to existing routes, and Connect, Reconnect and Disconnect work (oauth-connections spec plus the backend tests).
- **Secrets:** none reach the browser.
- **Status sources:** Eventbrite and Meetup status perform live read-only provider checks on page load (bounded timeouts, normalized to `UNAVAILABLE`). In production, LinkedIn and Instagram status is derived from vault metadata and configuration without network calls.
- **L2:** that production status omits the organization and account-ID checks and the HTTPS/secret-length rules that Approval readiness applies, so it can say "Connected" while Approval is blocked. H1 makes this worse until a restart.

## 18. Persistence and resource-binding assessment

- **Binding identity:** `destination_resource_bindings` is unique on `(destination, account_key, event_id)` and `(destination, account_key, external_id)`, keyed by local Event for every connector.
- **Account keys:**

  | Connector | `account_key` |
  |---|---|
  | Eventbrite | `organization:{id}` |
  | Meetup | member fingerprint |
  | LinkedIn | `organization:{urn}` |
  | Instagram | `professional-account:{id}` |

- **Rebinding:** a remote ID cannot be bound to two Events under one account, and one Event cannot hold two resources under one account. LinkedIn and Instagram additionally block when the Event is already bound under a different account. Eventbrite creates a new listing under a genuinely different organization, which is legitimate because it is a different account.
- **Model parity:** models match migrations, and the migration suite runs `alembic check`.
- **Legacy columns:** `approvals.approved_plan_hash` and `runtime_mode` and `destination_executions.approved_plan_hash` are retained, nullable legacy columns that are never used for authority.

## 19. Retry assessment

- **Scope:** retry is per execution and requires `FAILED` + `retryable`. Retryable is set only when no effect is confirmed, or when a bound resource makes a retry an update.
- **Siblings:** successful siblings are untouched (existing tests).
- **UNKNOWN:** an UNKNOWN row must be reconciled first. Reconciliation can only produce `FAILED` (confirmed absent, retryable), `SUCCEEDED` (verified readback) or UNKNOWN.
- **SUCCEEDED:** retry or Start is a no-op, and the E2E second Start makes no provider writes.
- **Gap:** retry on an orphaned `RUNNING` row returns 200 and silently does nothing (part of H3).

## 20. UNKNOWN assessment

- UNKNOWN is produced by adapter exceptions, missing results, unverifiable success evidence and explicit uncertain outcomes. External IDs are preserved where known.
- Reconciliation is verification-only and never searches by title.
- The UI shows "Verify destination result" and "Acknowledge uncertainty", and no Retry.
- UNKNOWN never counts as complete without an explicit `ABANDONED_UNCERTAIN` disposition, which yields `COMPLETED_WITH_ACKNOWLEDGED_UNCERTAINTY` and is surfaced as a later replacement risk.
- Unresolved UNKNOWN blocks successors, re-approval and material Event edits.

## 21. Completion assessment

`_execution_complete` and `_campaign_outcome` implement "no unresolved required action":

| Complete | Not complete |
|---|---|
| `SUCCEEDED` | `READY` |
| `MANUAL_DONE` | `RUNNING` |
| `SKIPPED` | `MANUAL_PENDING` without handoff |
| `FAILED` or `BLOCKED` with `ACKNOWLEDGED_FAILURE` | `FAILED` or `BLOCKED` unacknowledged |
| UNKNOWN with `ABANDONED_UNCERTAIN` | UNKNOWN unacknowledged |
| `MANUAL_PENDING` with `MANUAL_HANDOFF` | |

The semantics are correct. The defects are in reachability:

- H2: the UI cannot acknowledge or skip `BLOCKED` rows.
- H3: nothing can resolve `RUNNING`.
- The backend `manual-handoff` and `skip` actions are not exposed in the UI.

## 22. UI and backend contract assessment

- **Routes:** every frontend API path maps to a live backend route. No obsolete endpoints are called.
- **L3 (typing):** 36 generated response schemas are `unknown` because those routes return untyped dicts, covering campaigns, distribution, approval preview, groups and settings. `campaigns.tsx`, `facebookGroups.tsx` and `settings.tsx` hand-maintain those types, and only `App.tsx` imports generated types. No current field or enum mismatch was found in the traced fields, but nothing enforces the contract at compile time.
- **L1:** readiness messages lack LinkedIn and Instagram reason codes, so the operator sees the generic "Needs setup before this destination can be approved."
- **State coverage:** status labels cover every backend status. `RUNNING` and `BLOCKED` have no actions (H2, H3).
- **L10:** the replacement-acknowledgement list labels a prior LIVE publication "Published (Simulated)".
- **Optimistic UI:** none. Every action reloads distribution and preview from the server.

## 23. Refresh and reload assessment

All workflow state is server-side and re-fetched on mount: Event, clarifications, Campaign and versions, Group decisions (persisted per decision), Approval, executions, UNKNOWN, manual completion and OAuth status. The only React-only state is the unsubmitted Approval acknowledgement checkboxes, which is acceptable. A refresh never loses or fakes progress. It also cannot recover an orphaned `RUNNING` row (H3).

## 24. Concurrency assessment

| Race | Protection |
|---|---|
| Two Campaign revisions | Campaign row lock, expected revision, captured version ID |
| Two Approval requests | Campaign lock and revision check; same plan is idempotent |
| Two Start clicks | Campaign lock, unique execution identity and idempotency key, `RUNNING` claim check (E2E double Start: no duplicate writes) |
| Two retries | Execution `FOR UPDATE` and status check |
| Two manual completions | Disposition idempotency |
| Duplicate OAuth callback | Single-use state under `FOR UPDATE` (WP1M test, 8 threads) |
| OAuth reconnect | Atomic envelope replacement |
| Token refresh | Connection-then-envelope row locks (WP1M) |

L5 notes that readiness makes provider network calls while Campaign and Event locks are held (approve and start).

## 25. Migration assessment

- **Chain:** linear 0001–0009 with a single head. The migration tests cover an empty database to head, `alembic check` model parity, and downgrade/re-upgrade.
- **Constraints:** foreign keys are composite where needed (approval and execution to version scope, deferrable), and uniqueness supports every lookup used.
- **Downgrade caveat (I3):** `0007` downgrade re-creates `uq_approvals_campaign_version`. It will fail on real data that has more than one approval for a version, which is acceptable for a data-preserving production policy but worth documenting.

## 26. Security and authorization assessment

- **Login coverage:** every mutating route requires an app session, and `require_session` enforces exact Origin plus the CSRF token on unsafe methods. This covers:
  - Events, Campaigns, revisions and graphics
  - Group recommendations, decisions and selection
  - Approval, Start, retry, reconcile and dispositions
  - Settings, OAuth connect/disconnect and callbacks
- **Public routes:** health, auth start/callback/status, and the signed Instagram media GET/HEAD.
- **Test routes:** mounted only in test mode, which is now loopback-only.
- **Error contract:** errors are reduced to `{"error":{"code":…}}`. Validation errors become `INVALID_REQUEST`, SQL errors become `DEPENDENCY_UNAVAILABLE`, and unexpected errors become `REQUEST_FAILED` with a fixed log line.
- **No leaks found:** provider errors are normalized to categories and human text. OAuth failures redirect to fixed `/settings?connection=…` values. No stack traces, tokens, raw OAuth payloads, filesystem paths or SQL reach the client.

## 27. File and asset security assessment

- **Private storage:**
  - Keys are random (`[a-f0-9]{2}/[a-f0-9]{32}`).
  - Paths are confined to the storage root.
  - The default storage path is rejected if it falls under the frontend's public build or static directories.
  - Files are written with owner-only permissions (0600).
- **Intake source download** requires a session and both the Event and source IDs.
- **Campaign graphics** (`/api/campaign-versions/{id}/graphic`) require a session and serve only the version's manifest asset.
- **Signed Instagram endpoint:** HMAC token, TTL, exact version/asset/SHA-256, `PRIMARY_GRAPHIC` only, 404 otherwise. It cannot serve arbitrary Library or source files.
- **No arbitrary access:** there is no direct filesystem exposure, path traversal or ID-guessing path.
- **L8:** the default `private_storage_dir` is `<repo>/var/private`. It is gitignored and outside the source folders, but production does not require an explicit location.

## 28. Test architecture assessment

**Strengths:**

- The backend uses real PostgreSQL, real routes and services, and provider fakes at the HTTP transport layer. Adapters are not mocked above their layer.
- Assertions typically check domain results and provider-call counts, not just status codes.

**Weaknesses (M4):**

- **Fallback credentials hide H1.** The Playwright harness (`tests/browser_server.py`) injects `FakeLinkedInCredentials` and `FakeInstagramCredentials` as vault fallbacks, which makes `configured` true at startup. Its test mode-switch routes then call `rebuild()`. Both mask H1.
- **The mixed-mode spec doesn't use the real Eventbrite adapter.** It uses the `TestOnlyLiveModeAdapter` for Eventbrite LIVE, with LinkedIn and Instagram SIMULATED.
- **No cross-connector E2E existed** with all LIVE adapters and vault credentials together, so H1 was never exercised.
- **Defective states were asserted but never resolved.** The Instagram successor `BLOCKED` test checked only the row, never completion or the UI resolution path, which hid H2.
- **No crash or restart test** existed, which hid H3.
- **State isolation:** the `app` fixture truncates shared tables between tests. No leakage was observed.
- **Test-only behavior** is gated by `environment == "test"`, which is now loopback-only.

## 29. Operational configuration inventory

| Category | Variables |
|---|---|
| **Required for core app** | `EMP_ENVIRONMENT` (default `production`), `EMP_APP_ORIGIN`, `EMP_DATABASE_URL`, `EMP_GOOGLE_CLIENT_ID`, `EMP_GOOGLE_CLIENT_SECRET`, `EMP_ALLOWED_EMAILS`, `EMP_EXTRACTION_MODE`, `EMP_CAMPAIGN_GENERATION_MODE`, `EMP_IMAGE_GENERATION_MODE`, `EMP_GROUP_RECOMMENDATION_MODE` (production forces `openai`), `OPENAI_API_KEY`, `OPENAI_CAMPAIGN_MODEL`, `EMP_PRIVATE_STORAGE_DIR` (recommended explicit) |
| **Optional connector: Eventbrite** | `EMP_EVENTBRITE_EXECUTION_MODE`, `EVENTBRITE_PRIVATE_TOKEN`, `EVENTBRITE_ORGANIZATION_ID`, `EVENTBRITE_ORGANIZER_ID`, `EVENTBRITE_CURRENCY` |
| **Optional connector: Meetup** | `EMP_MEETUP_EXECUTION_MODE`, `MEETUP_CLIENT_KEY`, `MEETUP_AUTHORIZED_MEMBER_ID`, `MEETUP_SIGNING_KEY_ID`, `MEETUP_PRIVATE_KEY_PATH`, `MEETUP_NETWORK_URLNAME`, `MEETUP_GROUP_URLNAME` |
| **Optional connector: LinkedIn** | `EMP_LINKEDIN_ORGANIZATION_EXECUTION_MODE`, `LINKEDIN_API_VERSION`, `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI`, `LINKEDIN_ORGANIZATION_ID`, `LINKEDIN_ORGANIZATION_NAME`, `LINKEDIN_COMMUNITY_ACCESS_TIER` |
| **Optional connector: Instagram** | `EMP_INSTAGRAM_EXECUTION_MODE`, `INSTAGRAM_GRAPH_API_VERSION`, `INSTAGRAM_PROFESSIONAL_ACCOUNT_ID`, `INSTAGRAM_USERNAME`, `INSTAGRAM_CLIENT_ID`, `INSTAGRAM_CLIENT_SECRET`, `INSTAGRAM_REDIRECT_URI`, `PUBLIC_MEDIA_BASE_URL`, `MEDIA_DELIVERY_SIGNING_SECRET` |
| **Vault (needed for LinkedIn/Instagram LIVE)** | `CREDENTIAL_VAULT_KEYRING_PATH`, `CREDENTIAL_VAULT_ACTIVE_KEY_ID` |
| **Production secrets** | Google client secret, database password, `OPENAI_API_KEY`, Eventbrite token, Meetup private key file, LinkedIn/Instagram client secrets, media signing secret, vault keyring file |
| **Non-secret config** | Origins, IDs, URL names, modes, model IDs, API versions, redirect URIs, storage path |
| **Development/test only** | `EMP_ENVIRONMENT=test` (loopback only), `extraction_mode=fake` etc. (rejected in production), `dev_auth_enabled` (always rejected), test-only injection parameters on `create_app` (refused outside test) |

Observations:

- **Duplicate names (L8):** every connector variable accepts both an unprefixed name and a `EMP_` name (`AliasChoices`), and the unprefixed name wins if both are set.
- **Dangerous defaults:** none. Modes default to SIMULATED and environment defaults to production.
- **Connector enabled without config:** a LIVE mode with missing configuration is accepted at startup and blocks truthfully at readiness. Production does not validate that the vault keyring exists when LinkedIn or Instagram is LIVE; the vault reports unavailable at runtime, which is fail-closed (L8).

## 30. Documentation and code discrepancies

| Document | Statement | Actual |
|---|---|---|
| `approval-and-simulated-execution.md` (completion paragraph and the Destination-execution commands list) | Manual work can be completed, handed off, or explicitly skipped; failures and blocks can be acknowledged | The backend supports all of these, but the UI exposes only Mark complete, Retry, Account for final failure (`FAILED` only), Verify and Acknowledge uncertainty. Handoff, skip and `BLOCKED` acknowledgement are unreachable (H2). |
| `state-machine.md` (APPROVED/EXECUTING → COMPLETE) | User must acknowledge unresolved known failures or blocks | Not possible for blocks from the UI (H2). |
| `execution-safety.md` (lease paragraph) and `state-machine.md` (`RUNNING` row) | "Lease expiration after possible dispatch means UNKNOWN" | No lease exists. An orphaned `RUNNING` row stays `RUNNING` (H3). |
| `oauth-connections.md`, `linkedin-organization-connector.md`, `instagram-live-connector.md` | Connecting in Settings enables LIVE readiness | It requires a process restart (H1). Not documented. |
| `instagram-live-connector.md` (one-post lifecycle) | A successor blocks before mutation | True. The doc omits that the Approval preview reports `READY` and that the block can only be closed via the API (H2). |

The remaining connector, vault and mixed-mode docs were spot-checked and match the code.

## 31. Findings by severity and category

### HIGH

#### H1: LinkedIn/Instagram LIVE readiness is frozen at startup (WIRING)

- **Affected:** `CampaignExecutionService.rebuild` / `DestinationAdapterRegistry.__init__` (`simulated_connectors.py`); `LinkedInLiveAdapter.__init__` (`linkedin.py:734`) and `InstagramLiveAdapter.__init__` (`instagram.py:846`), which set `declaration.readiness_live` from `static_blockers(settings, credentials)`, including `credentials.configured`. `rebuild()` is called only in `CampaignExecutionService.__init__` (and by test-only routes).
- **User-visible behavior:** after a successful Connect in Settings, Approval still shows LinkedIn and Instagram as not ready, with the generic "Needs setup…" message (`DESTINATION_NOT_READY`), until the server restarts. Settings meanwhile says Connected.
- **Root cause:** the per-adapter declaration is computed once. `DestinationAdapterRegistry.readiness` trusts `declaration.readiness_live` before calling the dynamic `validate`.
- **Evidence:** a temporary probe showed the readiness reason move from `LINKEDIN_ACCOUNT_NOT_CONNECTED` to `DESTINATION_NOT_READY` after connect, and to `READY` only after `rebuild()`. The committed strict xfail `test_oauth_connection_makes_live_destinations_approvable_without_restart` reproduces it.
- **Safety consequence:** none (it fails closed). It does break the documented activation sequence.
- **Smallest correction:** compute LinkedIn and Instagram `declaration` dynamically (a property), or make the registry consult `adapter.blocker()` instead of the frozen declaration. Then remove the xfail.
- **Blocks:** later activation of real LinkedIn/Instagram credentials. It should be fixed before production hardening completes.

#### H2: BLOCKED destinations have no client resolution path (UI / DOMAIN)

- **Affected:** `frontend/src/campaigns.tsx` (ExecutionPanel destination cards: "Account for final failure" only for `FAILED` non-retryable, no Skip, no Handoff, Start hidden once rows exist); `_execution_complete` (`BLOCKED` requires `ACKNOWLEDGED_FAILURE`); readiness that doesn't predict execution-time blocks.
- **User-visible behavior:** the destination card shows "Blocked" with no button. The campaign stays "Action required" permanently. The only exit is creating a successor version, which itself can block again for Instagram.
- **Reproduced case (Instagram):** after a post is published, any copy revision previews Instagram `READY` / "Will publish live". Start produces `BLOCKED` (`INSTAGRAM_ALREADY_PUBLISHED_IMMUTABLE`), the outcome is `ACTION_REQUIRED`, and retry returns 409 `DESTINATION_NOT_RETRYABLE`. Only `POST /acknowledge-failure` (API) completes the campaign, as `COMPLETED_WITH_ACKNOWLEDGED_FAILURES`.
- **Other execution-time `BLOCKED` sources:**
  - LinkedIn: media replacement, organization changed, remote post changed, credential not connected or re-authorization needed at execution, scope or role missing.
  - Meetup: group change, bound Event not found, unsupported state, account mismatch.
  - Instagram: target changed, limit reached, credential unavailable.
  - Any destination: `APPROVED_CONNECTOR_MODE_UNAVAILABLE` at Start. The backend could revive it on a later Start, but the UI hides Start.
- **Evidence:** `test_provider_blocked_successor_has_only_an_api_resolution_path` (backend, passes and pins the behavior); `campaigns.test.tsx` "audit: a provider-blocked destination currently has no resolution action" (frontend, pins zero buttons).
- **Safety consequence:** none. Nothing is published wrongly. Workflow completion is impossible in the UI for ordinary, intended safety blocks.
- **Smallest correction:**
  1. Show "Account for this" (`acknowledge-failure`) and optionally "Skip" on `BLOCKED` cards, and "Try again" (re-Start) for `APPROVED_CONNECTOR_MODE_UNAVAILABLE`.
  2. Make Instagram readiness consult the existing Event binding so the preview shows the block before approval.

  Invert the pinned assertions when fixed.
- **Blocks:** production hardening (core workflow completion).

#### H3: an orphaned RUNNING attempt has no recovery (PERSISTENCE / DOMAIN / OPERATIONS)

- **Affected:** `_claim_auto_attempt` (returns `None` for `RUNNING`), `_claim_reconciliation` (UNKNOWN only), `_human_disposition` (no `RUNNING` actions), `prepare_for_successor` and `invalidate_material_event_approvals` (both refuse while anything is `RUNNING`), and approval blockers. No lease or recovery exists anywhere (`grep lease` returns nothing).
- **User-visible behavior:** the card reads "In progress" forever. Retry returns 200 and silently does nothing. Verify does nothing. Acknowledge and Skip return 409. Revisions return 409 `EXECUTION_UNRESOLVED_BEFORE_REVISION`, and any material Event edit returns 409 `EVENT_CHANGE_BLOCKED_BY_UNRESOLVED_EXECUTION`. Recovery requires direct database intervention.
- **Root cause:** an attempt is claimed and committed as `RUNNING` before provider dispatch and finalized in a later transaction. Process loss in between (deploy restart, crash, OOM) is never detected.
- **Evidence:** `test_process_loss_during_dispatch_leaves_running_with_no_recovery_path` claims an attempt, "loses" the process, restarts the app, and asserts each dead end.
- **Safety consequence:** safe with respect to duplicates (it never re-dispatches), but the campaign and its Event are wedged. The design docs promise lease expiry, after possible dispatch, leads to UNKNOWN; that is not implemented.
- **Smallest correction:** a bounded stale-`RUNNING` recovery, for example a lease timestamp on the attempt. A `RUNNING` attempt older than its lease becomes UNKNOWN (`possible_dispatch`), on Start, reconcile or a startup sweep, so the existing reconcile and acknowledge paths apply.
- **Blocks:** production hardening (deploys and restarts are routine).

### MEDIUM

#### M1: Approval is not bound to the exact preview the operator saw (DOMAIN / UI)

- **Affected:** `ApproveVersion` (`execution_routes.py`), `approve_version`, `campaigns.tsx` approve call.
- **Behavior:** the preview shows a server-derived plan and hash. The approve request sends `campaign_version_id` + `version_hash` + acknowledgements, and the server rebuilds the plan from registry state at approval time. The UI never shows target identity (LinkedIn Page, Instagram account, Meetup Group, Eventbrite organization).
- **Race / configuration implication:** if configuration changes between preview and approve (in practice a restart or deploy with different target settings or modes), the operator approves a different plan than the one displayed. The persisted approval is still internally consistent and immutable, and execution honors it or blocks.
- **Evidence:** code; the Playwright mixed-mode spec asserts the body has no `destination_plan_hash`.
- **Safety consequence:** low probability; an authorization-integrity gap rather than a wrong-content risk.
- **Smallest correction:** add a required `destination_plan_hash` to `ApproveVersion`, sent from the preview. The server rejects with `DESTINATION_PLAN_CHANGED` if its rebuilt plan hash differs. Display target names in the preview.
- **Blocks:** production hardening (recommended before real credentials).

#### M2: synchronous LIVE dispatch inside the Start request (OPERATIONS)

Start runs every destination sequentially in one HTTP request:

- Eventbrite: about ten calls
- LinkedIn: image polling
- Instagram: container polling and publish
- Credential refresh

Long requests interact with proxy timeouts and make H3 more likely. Correction: keep it bounded now, and move to supervised background execution (the existing design anticipates it) after H3 is fixed. This does not block hardening if H3 is fixed; it is a production-scale concern.

#### M3: test architecture masks wiring defects (TEST)

See §28: fallback credentials and `rebuild()` in the browser harness, the test-only Eventbrite LIVE double in mixed-mode browser coverage, and no completion assertion after `BLOCKED`. Correction: run at least one browser flow with vault-only credentials and no `rebuild()`, and assert completion or resolution in blocked scenarios. The new backend E2E covers the cross-connector path.

### LOW

| ID | Category | Finding | Smallest correction |
|---|---|---|---|
| L1 | UI | `readinessText` lacks LinkedIn/Instagram reason codes, so the message is generic | Add messages for the codes in `settings_routes.REASON_TEXT` |
| L2 | UI/WIRING | Production Settings status for LinkedIn/Instagram differs from Approval readiness (organization or account ID missing, HTTPS, signing-secret length) | Reuse `static_blockers` in the Settings route |
| L3 | UI/TEST | 36 response schemas are untyped (`unknown`), and frontend types are hand-maintained | Add response models incrementally |
| L4 | DOMAIN | One unavailable Facebook Group fails the whole Start (all destinations) | Block only the Facebook entry |
| L5 | OPERATIONS | Readiness performs LinkedIn/Instagram network calls (role, account, quota, possible refresh) during preview, and during approve and Start while Campaign and Event locks are held | Acceptable now; move live probes outside lock scope later |
| L6 | PERSISTENCE | (Inferred) re-approving a plan identical to an earlier approval whose rows were all `FAILED` could reuse the idempotency key, causing a unique violation that surfaces as 503 | Include the approval ID in the idempotency key, or handle the conflict |
| L7 | WIRING | Dead or legacy code: see the classification list below | Clean up in a later pass |
| L8 | OPERATIONS | Dual env names (unprefixed wins); default private storage inside the repo tree; no production startup check that the vault keyring exists when LinkedIn/Instagram are LIVE | Document; optionally add production validation |
| L9 | DOCUMENTATION | §30 discrepancies | Update docs with the fixes |
| L10 | UI | Replacement-acknowledgement labels call `destinationStatus(item.status)` without the runtime mode, so a prior **LIVE** verified publication is shown as "Published (Simulated)" (observed in the Meetup Playwright snapshot). This is a Practice/Live mislabel on the exact screen where the operator confirms a public replacement. | Include `runtime_mode` in `replacement_requirements` and pass it to `destinationStatus` |
| L11 | TEST | `campaign-meetup-live.spec.ts` `approveAndStart` races the preview refresh, failing intermittently (2/2 in the full run, 1/2 then 0/2 in isolated reruns) | Wait for the acknowledgement checkboxes (or `can_approve`) before checking them |

L7 dead and legacy code classification:

- Production `_normalize_execute_result` and reconciliation accept `test-live:` results. **Inert:** only `TestOnlyLiveModeAdapter` produces them, and it cannot be constructed outside test mode.
- `manual-group-selection-deferred`, "WP1E" wording, and a LIVE Eventbrite entry without an account labelled `simulated-default`. **Confusing but inert.**
- Legacy `approved_plan_hash` and `runtime_mode` columns. **Harmless.**
- Backend `skip` and `manual-handoff` dispositions, and `BLOCKED → READY` revival on Start. **Reachable by API only** (not from the UI).

### INFORMATIONAL

- **I1:** the default `BLOCKED` message in `_normalize_execute_result` is Meetup-specific, but every adapter supplies its own `human_error`.
- **I2:** with Meetup LIVE configured, any campaign with a graphic is unapprovable. Surfaces are fixed at campaign creation, so only an operator mode change resolves it. This is a documented deferral.
- **I3:** the `0007` downgrade cannot run on data with multiple approvals per version.
- **I4:** Group decisions are frozen into every successor by design.
- **I5:** Eventbrite and Meetup Settings cards make live read-only calls on page load.

## 32. Tests added

| Test | Purpose | Result |
|---|---|---|
| `backend/tests/test_system_integration_audit.py::test_cross_connector_end_to_end_mixed_workflow_with_vault_credentials` | Full workflow (§5) using `create_app` wiring, vault-only credentials via Settings OAuth, real LIVE adapters over fakes, two process restarts, exact-graphic checks for every provider, manual completion, double-Start idempotency | Pass |
| `…::test_oauth_connection_makes_live_destinations_approvable_without_restart` | H1 reproduction | Strict xfail (fails today; flips on fix) |
| `…::test_provider_blocked_successor_has_only_an_api_resolution_path` | H2 backend reproduction | Pass (pins current behavior) |
| `…::test_process_loss_during_dispatch_leaves_running_with_no_recovery_path` | H3 reproduction | Pass (pins current behavior) |
| `frontend/src/campaigns.test.tsx` "audit: a provider-blocked destination currently has no resolution action" | H2 UI reproduction | Pass (pins zero actions) |

When H2 and H3 are fixed, the pinned assertions must be inverted, and the H1 xfail removed.

## 33. Full validation results

Run on the final audit tree (application code unchanged from `0e3239c`; audit tests and this report added):

| Check | Result |
|---|---|
| Backend pytest (PostgreSQL) | **282 passed, 1 xfailed** (279 baseline + 3 audit tests passing + 1 strict xfail for H1) |
| Migration verification | Included in the backend suite: empty DB → head, `alembic check`, downgrade/re-upgrade, all passing |
| Ruff check / format | All checks passed; 76 files already formatted |
| pip-audit (backend environment) | No known vulnerabilities (local package skipped as not on PyPI) |
| Frontend unit (Vitest) | **56 passed** (55 baseline + 1 audit test). The first run of the new test failed on a selector error in the test itself (it matched the copy-preview heading); it was corrected to select the destination card and then passed. |
| Frontend type generation | `npm run types` regenerated `api.generated.ts` with no diff (in sync with backend OpenAPI) |
| ESLint + `tsc -b` | Passed |
| Prettier check | Passed |
| Production build (`tsc -b && vite build`) | Passed |
| npm audit | 0 vulnerabilities |
| Playwright desktop + mobile (full suite) | **48 passed, 2 failed, 8 did not run.** Both failures were `campaign-meetup-live.spec.ts › Meetup creates a draft, publishes after verification, and updates the same Event`; the "did not run" tests are the rest of that serial spec. |
| Playwright rerun of that spec | Run 1 (both projects): 5 passed, 1 failed (desktop), 4 did not run. Runs 2 and 3 (desktop only): 5/5 passed each time. |
| `git diff --check` | Clean |
| Secret scan of the diff | No credential material (only synthetic test constants) |
| Source integrity | PASS |

The Meetup failure is a flaky test race (L11), not an application regression. The spec's `approveAndStart` collects the replacement-acknowledgement checkboxes before the post-revision preview has rendered them, so it checks none and **Approve this version** correctly stays disabled. The page snapshot confirms an unchecked acknowledgement and a disabled button. The audit changed no Meetup or Playwright code. The same snapshot exposed a real labelling bug (L10).

## 34. Known deferred provider and account verification

- **Eventbrite:** real-account create, update and publish verification is deferred (WP1I).
- **Meetup:** real JWT owner flow and draft/publish verified only against fakes. Photo upload is deferred, so graphics block LIVE.
- **LinkedIn:** real Community Management Standard access, Page role, and Posts/Images behavior are unverified.
- **Instagram:** real Instagram Login app review, Professional account, container processing and `media_publish` are unverified.
- **OAuth:** real LinkedIn/Meta token shapes, refresh rotation and revocation are unverified.

All five are verified only against no-network fakes. No real the operator account was connected.

## 35. Recommendation

**C. NOT READY — MATERIAL INTEGRATION DEFECTS FOUND**

The architecture is sound, and the safety-critical integration was verified end to end:

- exact version, exact plan, frozen targets and exact graphic
- vault-only credentials
- UNKNOWN discipline and idempotent execution

No defect lets the application publish the wrong content, version or graphic, reach the wrong account, or duplicate a post. However, three HIGH defects, each reproduced by a committed test, prevent ordinary workflows from completing through the product:

- **H1:** a Settings OAuth connection requires a server restart before LinkedIn or Instagram can be approved.
- **H2:** an intended provider safety block leaves the Campaign permanently "Action required", with no UI resolution.
- **H3:** process loss during dispatch wedges the Campaign and blocks every edit to its Event.

Each has a small, well-bounded correction (§31). Fix H1–H3, and preferably M1, then re-run this audit's tests (inverting the pinned assertions) before production hardening.

### Answers to the critical questions

1. **Is every production connector reachable from the orchestrator?** Yes. All four LIVE adapters are built by `create_app` and selected per approved entry. The E2E test proves Eventbrite, LinkedIn and Instagram LIVE together with Meetup SIMULATED. Meetup LIVE is reachable for graphic-free campaigns (existing tests). Caveat: H1.
2. **Can the browser invoke a stale simulated path when Approval says LIVE?** No. The adapter is chosen from the stored entry's `runtime_mode`. A LIVE entry without a LIVE adapter blocks. The no-network LIVE double exists only in test mode.
3. **Can any execution use an unapproved CampaignVersion?** No. Claim re-checks the active approval, current version, both hashes and staleness.
4. **Can any connector use the wrong graphic?** No. The exact version manifest plus asset ID and SHA-256 are checked, and the bytes re-hashed.
5. **Can any connector publish to a target other than the Approval target?** No. Target drift blocks. Meetup honors the approved Group, and blocks on an account change.
6. **Can configuration drift alter an existing Approval?** No. Approvals are immutable, and drift can only block. A SIMULATED entry stays SIMULATED.
7. **Can an OAuth reconnect redirect an existing Approval to a different account?** No. Instagram rejects a different account and re-verifies `/me` at execution. LinkedIn re-checks the member's role on the frozen organization.
8. **Can retries duplicate external resources?** Not through the application's retry paths. Retry requires confirmed no effect or a bound resource to update. Instagram allows one post per Event. A duplicate is possible only after an explicit, recorded uncertainty acknowledgement.
9. **Can UNKNOWN become SUCCESS or COMPLETE accidentally?** No. It needs verified readback, or an explicit acknowledgement that is labelled as such.
10. **Can Instagram's signed endpoint expose unrelated private assets?** No.
11. **Can Facebook Groups auto-post?** No. The manual adapter cannot execute, and MANUAL entries are never dispatched.
12. **Can Meetup silently omit an approved graphic?** No. Any graphic blocks before mutation.
13. **Can LinkedIn retain or replace the wrong graphic?** No. A graphic change blocks, and image reuse requires a matching SHA-256 and organization.
14. **Can Instagram automatically duplicate a published post?** No. Any binding blocks before provider calls, and an uncertain publish is UNKNOWN and not retryable.
15. **Does disconnect prevent credential retrieval?** Yes, immediately (the envelope is deleted in the same transaction, with no cache).
16. **Does browser refresh preserve workflow state?** Yes. All state is server-side. It cannot repair H3.
17. **Can two simultaneous Start actions duplicate work?** No. Locks, unique identities and the `RUNNING` claim check prevent it (E2E double Start verified).
18. **Can an empty database migrate cleanly?** Yes (see §33 migration results).
19. **Do frontend and backend agree on every workflow state?** Statuses and outcomes are mostly labelled consistently, with one Live/Simulated mislabel in replacement acknowledgements (L10). The UI also lacks actions for `BLOCKED` and `RUNNING`, does not expose `skip` or `manual-handoff`, lacks LinkedIn/Instagram readiness messages, and its types are hand-maintained (H2, H3, L1, L3, L10).
20. **Is there code that exists but is not genuinely wired in?** Yes:
    - backend `skip` and `manual-handoff` dispositions
    - `acknowledge-failure` for `BLOCKED`
    - `BLOCKED → READY` revival on a later Start (UI hides Start)
    - the `test-live:` normalization branch (test-only)
    - dynamic `blocker()` checks that are shadowed by the startup-frozen declarations (H1)

### WP1M-R2 remediation status (2026-10-08)

Historical findings above are retained unchanged. WP1M-R2 addresses H1, H2, H3 and M1, plus L10 and L11. The remediation report records implementation and verification details. Post-fix validation: PostgreSQL backend 285/285, frontend 57/57, and full desktop/mobile Playwright 58/58 on two consecutive runs. No real provider calls/writes were made. Deferred findings include M2, L4, L6, and unrelated LOW cleanup. The remediation evidence supports moving the integration audit recommendation from C to B, subject to Architect review; this does not claim production readiness.
