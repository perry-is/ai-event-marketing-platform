# WP0B architecture review report

**Historical proposal, preserved for traceability.** WP0B was accepted in principle; its cardinality, approval/start transaction, Event-revision invalidation, capability/readiness and completion rules are superseded by [WP0C corrections](work-package-0c-report.md). The current linked contract documents include those corrections. The original proposal/report below is not the current implementation authority.

Proposed core contract, 2026-10-05. Starting clean HEAD `d8c1ac2e416b688c7bc4e619b43bb2de5cb7025b`; earlier baseline `92828ec096ae0c6cb3af6c2129e2533d719b8ece`. All decisions below are reviewable engineering proposals; no application implementation, deployment, provider call, live Drive access or push belongs to this package.

| Requested review item | Proposal |
| --- | --- |
| 1. Core architecture | One private responsive PWA/FastAPI application, PostgreSQL and private files. Deterministic domain services authorize/validate/transition; AI proposes; connectors translate approved intent. |
| 2. Canonical Event | One occurrence; title, start/timezone, delivery required for actual campaign approval; end/location/virtual instructions/pricing/people/content facts conditional or optional. Draft accepts incomplete information. No recurrence scheduler/default inference/post-event model in Phase 1. |
| 3. Campaign/version | One Campaign per Event; mutable workflow/draft request, ordered append-only snapshots containing facts used, content, selected targets/groups, semantic settings and immutable asset hashes. |
| 4. Exact approval | Explicit authenticated approval of current version id/hash/Event revision/runtime mode after readiness checks. Approval + per-target units committed atomically. Historical approval never authorizes a successor; material edit clears active authority and requires a new snapshot/approval. |
| 5. State machine | DRAFT (including clarification), GENERATING, REVIEW, APPROVED, EXECUTING, COMPLETE, ABANDONED. Generation failure preserves prior history and returns DRAFT. COMPLETE means all targets accounted for, with manual pending/acknowledged failures visible; UNKNOWN blocks closure. |
| 6. Capabilities | AUTO/MANUAL/UNAVAILABLE plus SIMULATED/LIVE and small verification/safe-retry declarations. Phase 1 fake Eventbrite/Meetup/LinkedIn organization/Instagram; Groups MANUAL; personal LinkedIn/Portal unavailable pending feasibility; Page excluded. No claim of live platform support. |
| 7. Execution safety | Unique version/destination/target/mode identity, stable idempotency key, leased attempt before dispatch, immutable final evidence, one-target retry. Timeout-after-possible-create is UNKNOWN requiring verification/reconciliation, never blind retry. |
| 8. Authentication | Proposed allowlisted Google OIDC, opaque database-backed secure-cookie sessions, single privilege level/no public signup. No Drive scopes/publisher tokens. Existing suitable account ASSUMED; operator provisioning needed. |
| 9. Database | PostgreSQL, SQLAlchemy 2/Psycopg, Alembic; fifteen focused tables with ownership/immutability/CAS/lock guards. JSONB for nested facts/semantic snapshots, no SaaS schema. |
| 10. Frontend | React/TypeScript/Vite, React Router, CSS modules; responsive review and simple action/status interface. Static-shell PWA only; no offline mutations/private cache. |
| 11. Sources/files | Preserve exact originals separately from accepted facts and proposals, practical locators, private UUID-keyed storage/hashes, bounded TXT/images/PDF intake and separate XLSX catalog import. No URL fetching or live Drive access. |
| 12. Groups | Preserve 184 source rows and all raw links/coordinates; local candidate UUID is not Facebook identity. Curated display/geography/audience/notes/scoped restrictions/review status; deterministic filter → model relevance → human decision → manual package. No automatic dedup/permissions/learning. |
| 13. Unresolved assumptions | One occurrence/Campaign; conditional readiness rules; limits/retention/session defaults; suitable identity provider/account; approval immediately dispatches; limited declared returned-link binding. All subject to Architect/client review, never historical defaults. |
| 14. Client questions | Normal destination/registration choices; suggested timezone; approval-to-distribution/correction behavior; working group reference/promotion rules for selected targets. See Phase 1 short list. No technical database or 56/26 question. |
| 15. Phase 1 sequence | P1-A private shell/auth/database → P1-B safe intake/fact clarification → P1-C version/review/groups → P1-D exact simulated approval/execution/retry/manual/history. Architect reviews before individual implementation prompts. |
| 16. Evidence changes | No legacy Event-schema migration/required 26-column form, no measured recurring/default library; sparse planner is task evidence; conflicted Group rows retain identity uncertainty; reference graphics are not approved universal templates. |
| 17. Files | Thirteen contract/plan documents plus this report created; README/open questions updated. See inventory below. Originals and previous audits/derived ledgers preserved. |
| 18. Validation | Package checks/results recorded below; no application tests can pass for an unimplemented app. Test strategy defines Phase 1 invariant acceptance. |
| 19. Deliberately deferred | Real platform schemas/OAuth/connectors/capability claims; model/model-price selection; production provider/region/cost; recurrence/templates/learning; public brand approval; live retention confirmation; remote correction/edit/delete semantics. No implementation authorized. |

## Documentation inventory

Created: domain-model.md; data-model.md; campaign-versioning.md; state-machine.md; connector-contract.md; execution-safety.md; source-material.md; group-model.md; authentication.md; application-boundaries.md; architecture.md; test-strategy.md; phase-1-plan.md; work-package-0b-report.md (all under docs/).

Modified: README.md and docs/open-questions.md. Evidence/source originals and existing analysis scripts remain unchanged. The contract link map is in [architecture.md](architecture.md); [Phase 1 plan](phase-1-plan.md) is a proposal, not a released implementation prompt.

## Review and validation record

Reviewed the package for consistency across snapshot ownership/immutability, exact mode-bound approval, supersession/cancellation races, evidence-only reconciliation, partial/manual completion, returned-link binding and group identity uncertainty. Final checks passed: `scripts/verify-source-audit.py` reconciles independent OOXML counts/denominators and all four supplied-document/workbook plus six JPEG hashes; backend/frontend remain placeholders only. File-set review confirms exactly sixteen Markdown changes and no source/reference/script/application edits; targeted secret-pattern scan found no likely secrets; all 76 local Markdown links/anchors resolve; newline/whitespace checks and `git diff --check` passed. Commit hash and final status are reported in the Architect handoff. No live API, app or deployment tests were run; no correctness claim is made for unimplemented software.

**Evidence limitation:** historical Events Master claim remains UNVERIFIED/POSSIBLY INCORRECT. No historical event count, field completeness, recurrence or defaults were invented. Verified current evidence and explicit requirements inform this proposal; narrative 26-field concepts cannot dictate the canonical schema.
