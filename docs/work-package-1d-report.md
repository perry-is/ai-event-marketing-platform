# Work Package 1D — Campaign review and immutable versioning

**Status: IMPLEMENTATION COMPLETE; final verification results recorded below.** This package occupies the reviewed P1-C campaign/versioning slot. It demonstrates campaign generation, focused revision, stale-event detection, and version history using deterministic local rules. It does not use live AI for marketing content, publish content, approve a campaign, or execute destinations.

## Implementation

PostgreSQL now contains `campaigns` and `campaign_versions`, added by Alembic revision `0003_campaigns`. An Event can own multiple Campaign rows; no Event-level unique constraint was added. The current user flow creates/reuses one primary campaign per Event. A Campaign tracks its current review version and pre-approval `REVIEW` state. CampaignVersion records the parent, sequential version number, source Event revision, complete material Event snapshot, selected content surfaces, validated semantic copy, revision request/targets, deterministic generator identity, canonical snapshot text, content hash, creator, and timestamp.

CampaignVersion snapshots are immutable at the database boundary: a PostgreSQL trigger rejects updates and deletes. Generation and revision services run outside database transactions. The final write locks Event then Campaign, rechecks the captured material Event facts and campaign revision, then inserts a successor and updates the Campaign's current-version pointer atomically. If material Event facts change while content is being prepared, the write is rejected and no version is created. Failed or unsupported generation/revision leaves existing history unchanged. The Event revision is retained for provenance; staleness compares material facts, so an unrelated internal note/revision increment does not mark a campaign stale. A material Event edit marks the current campaign stale without changing the old version. Revision is blocked until explicit regeneration inserts a successor against the current Event.

The fake generator is isolated behind a generation protocol; deterministic revision rules sit behind a separate revision protocol. Campaign output uses only selected review surfaces: Eventbrite, Meetup, LinkedIn organization, LinkedIn personal, Instagram, and Facebook Groups manual copy. It reflects known title, schedule/timezone, format, location, topic, host/presenter, registration, and price facts where relevant and never fills a missing fact with an invented value. The hash is SHA-256 over canonical UTF-8 JSON of material Event facts, selected surfaces, and content; database identifiers, Event revision, timestamps, and generator metadata are excluded.

Authenticated API operations create/get a Campaign for an Event, retrieve a Campaign and historical version, generate the initial version, request a deterministic revision, and explicitly regenerate a stale Campaign. Every route remains behind the WP1A session and CSRF/origin boundary. The backend rejects Event-fact changes posed as copy revisions and rejects revisions outside its deliberately small command set. There are no approval, execution, destination account, delivery, or publishing endpoints.

The frontend adds an Event-linked campaign review screen. It shows shared Event facts once, selected listing/social copy, text-only creative direction, current-version status, read-only history, stale warnings, and revision requests. It labels output as deterministic sample content and states that nothing is published. The page works in desktop and iPhone-sized E2E runs.

## Verification

Final acceptance results:

| Check | Result |
| --- | --- |
| PostgreSQL-backed backend suite | **98 passed** |
| Empty-database migration chain, repeated upgrade, downgrade, and re-upgrade | **Passed** in the backend suite |
| Frontend unit tests | **17 passed** |
| Playwright E2E | **6 passed** across desktop Chrome and iPhone-sized Chromium |
| Python Ruff formatting and lint | **Passed** |
| Frontend Prettier, ESLint/TypeScript, generated API types, production build | **Passed** |
| npm dependency audit | **0 vulnerabilities** |
| Python dependency audit | **No known vulnerabilities** |
| `git diff --check` | **Passed** |
| Changed-file secret scan | **Passed; no credentials or secret values found** |
| Source/reference integrity | **Passed; supplied originals remain unchanged** |

The browser test uses the existing safe mocked OIDC path and PostgreSQL. It creates a ready Event, generates Version 1, makes a LinkedIn-only tone revision, checks that Eventbrite copy is unchanged, opens Version 1 read-only, rejects a requested date change through the Campaign path, changes the Event, sees the stale warning, explicitly regenerates Version 3, and reopens the Event and campaign from the Events list. It runs at both configured viewports. The migration test starts from an empty PostgreSQL database and exercises the full chain, repeat upgrade, downgrade, and re-upgrade.

## Files changed

- Backend: `backend/app/models.py`, `backend/app/main.py`, `backend/app/campaign_service.py`, `backend/app/campaign_routes.py`, `backend/migrations/versions/0003_campaigns.py`.
- Backend tests: `backend/tests/conftest.py`, `backend/tests/test_campaigns.py`, `backend/tests/test_foundation.py`.
- Frontend: `frontend/src/App.tsx`, `frontend/src/events.tsx`, `frontend/src/campaigns.tsx`, `frontend/src/campaigns.test.tsx`, `frontend/src/App.module.css`, `frontend/src/api.generated.ts`, `frontend/e2e/campaign-review.spec.ts`.
- Documentation: `README.md`, `docs/campaign-simulation.md`, this report.

No new runtime or development dependency was introduced. Source/reference originals and WP1C extraction behavior remain unchanged. No architecture conflict was found; WP1D follows the corrected scope and reviewed versioning boundary.

## Explicitly deferred

Real OpenAI marketing generation, campaign prompts/model routing, image generation, Facebook Group recommendations or selection, destination-specific account validation, connectors, approval, execution, retries, external IDs/URLs, and real publishing remain out of scope. Live Google OIDC was not exercised because provider credentials remain intentionally unconfigured; automated mocked OIDC coverage remains in place. No push was made.
