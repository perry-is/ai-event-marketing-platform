# Work Package 1I report: live Eventbrite connector

**Status: COMPLETE.**

- Live read-only smoke test: **DEFERRED** (operator check on 2026-10-06 found no Eventbrite credentials/configuration in the local environment).
- Optional live write test: **NOT RUN**.

## Operator live verification — 2026-10-06

The existing `python -m scripts.eventbrite_smoke` command was run from `backend` against the current `e9d259b` checkout. It returned `read_only_smoke: DEFERRED` with the sanitized missing-configuration reasons `EVENTBRITE_TOKEN_MISSING`, `EVENTBRITE_ORGANIZATION_MISSING`, `EVENTBRITE_ORGANIZER_MISSING`, and `EVENTBRITE_CURRENCY_MISSING`. The script exits before constructing the HTTP client when any of those requirements is missing, so this run made **no Eventbrite API requests** and cannot verify token authentication, account identity, Organization access, Organizer validity, or LIVE readiness.

The root `.env` contains no Eventbrite configuration keys; no Eventbrite variables were present in process, user, or machine environment configuration. The explicit `EVENTBRITE_ENABLE_LIVE_WRITE_TEST=1` opt-in is absent. Accordingly, the draft write test was **NOT RUN**. No synthetic draft was created, there is no external Event ID, and deletion is not applicable. Real API compatibility remains unverified pending private operator configuration of the required credentials and account identifiers.

Baseline: clean tree at `e9a14bf3245264b9ff886ee962e661f79c00d36b` (WP1I-A mixed-mode approvals). Source integrity passed before and after. The operator's live Google Drive was not accessed. The full design is in [live Eventbrite connector](eventbrite-live-connector.md); the [mixed-mode architecture](mixed-mode-approval.md) was not rewritten.

## Summary

- **Live adapter:** the real Eventbrite API v3 adapter (`app/eventbrite.py`) is one entry in WP1I-A's immutable mixed-mode plans.
  - The browser never chooses LIVE/SIMULATED, CREATE/UPDATE, credentials or external IDs.
  - New plans derive Eventbrite LIVE from trusted configuration and freeze the account and exact listing facts into the destination-plan hash. The CampaignVersion hash is unchanged by mode.
  - Execution uses only the stored plan entry. Configuration drift blocks; it never simulates.
- **Simulated adapter:** unchanged. The WP1I-A no-network LIVE-shaped test double remains for its existing tests.
- **Auth:** a server-side private token (`EVENTBRITE_PRIVATE_TOKEN`) behind an isolated `CredentialProvider`. OAuth is deferred.
  - Required configuration: `EVENTBRITE_ORGANIZATION_ID`, `EVENTBRITE_ORGANIZER_ID`, `EVENTBRITE_CURRENCY`.
  - Missing values block live approval with specific reasons. There is no fallback to simulation.
- **Readiness:**
  - Local and deterministic for approval, start and retry: configuration, account match and renderer acceptance.
  - Live read-only for Settings: token, current user, organization and organizer.
- **Settings UI:** shows Connected/Needs setup/Unavailable, the organization and organizer names, and plain blockers. It never shows the token, IDs, headers or provider payloads.

## Remote identity: create versus update

- **Binding:** migration `0008_resource_bindings` adds `destination_resource_bindings`, one remote listing per (destination, account, local Event).
  - Unique in both directions, so no other Event can claim a listing.
  - The identity is immutable and cannot be deleted (trigger).
  - It stores the verified URL, state, reusable venue/ticket/logo metadata and timestamps. No token or OAuth data is stored.
- **CREATE versus UPDATE:** no binding means CREATE; a binding means UPDATE of the same Eventbrite event. A newly created ID is bound in its own transaction before any further remote step.
- **Successor versions:** an approved successor version updates the same listing and reuses its ticket class. Historical execution stays intact.

## Safety

- **Draft-first order:** venue → draft create or update → ticket → structured-content description → exact approved logo → publish (only if not already live) → verification read.
- **Success:** requires the verification read to show the bound ID, the approved name/start/end, status live, and an `eventbrite.*` HTTPS URL.
- **UNKNOWN:** an uncertain CREATE (timeout after possible acceptance, 5xx, unparseable success, or bind failure) becomes UNKNOWN.
  - Retry is refused.
  - Reconciliation reads by known ID only and never searches by title or date.
  - With no ID, it stays UNKNOWN for explicit human disposition.
- **Error categories:** `AUTHENTICATION`, `PERMISSION`, `NOT_FOUND`, `RATE_LIMIT`, `VALIDATION`, `NETWORK`, `REMOTE_SERVER`, `CONFIGURATION` and `UNKNOWN_OUTCOME`/`AMBIGUOUS_OUTCOME`, plus Eventbrite's stable error code as diagnostics.
- **Retries:** no automatic retries. Explicit retry acts on Eventbrite only.
- **No invented facts:**
  - **Capacity:** Eventbrite requires `quantity_total`. Capacity is a new **correction-only** Event fact (`facts.registration.capacity`, 1 to 1,000,000; AI extraction cannot set it). A missing capacity blocks live approval.
  - **Paid tickets:** blocked. There is no approved price entry or verified payout setup, and paid is never turned into free.
  - **Schedule:** a missing end time blocks.
  - **In-person and hybrid:** need a venue name plus a strict `Street, City, ST 12345` address.
  - **Virtual:** creates no venue.
  - **Venue reuse:** only on an exact normalized name and address match.
- **Approved graphic:** the exact approved asset bytes, checked by asset ID and SHA-256 against that version's manifest. They are uploaded to the provider URL without the credential. A newer unapproved graphic is never used.
- **Mixed-mode UI:** shows Eventbrite as "Published · Live on Eventbrite · verified" with a View on Eventbrite link, and siblings as "Published (Simulated) · Practice mode" or manual. A drift-blocked live entry states that nothing was sent and it wasn't switched to practice mode. Approval preview shows plain-language Eventbrite blockers.
- **Event details:** the correction form gains Street address, Admission (free/paid) and Capacity.

## Results

| Check | Result |
|---|---|
| Backend (PostgreSQL) | **205 passed, 0 failed**: the 183 baseline plus 22 new, in `test_eventbrite_live.py` and `test_eventbrite_units.py` |
| Frontend unit tests | **45 passed, 0 failed**: 38 baseline plus 7 new (Settings states, mixed live/practice/manual display, verified link, UNKNOWN, drift blocking, no credential exposure) |
| Lint, format, type generation, production build | passed |
| Playwright | **30 passed, 0 failed** (15 per project, desktop Chromium and iPhone 13) |
| Ruff check and format | passed |
| `npm audit` | 0 vulnerabilities |
| pip-audit | no known vulnerabilities |
| `git diff --check` | passed |
| Changed-file secret scan | only the synthetic test token constant found |
| Source integrity | PASS |

The 22 new backend tests cover:

- readiness states and credentials never returned
- anonymous rejection
- mixed LIVE/SIMULATED/MANUAL plans
- create-once with binding and duplicate-start protection
- binding identity immutability
- uncertain CREATE → UNKNOWN with no retry and no duplicate
- successor UPDATE of the same listing
- virtual no-venue, strong versus weak venue reuse
- capacity, paid and address blockers
- exact approved graphic bytes with no credential sent to storage
- a newer unapproved graphic not publishable
- publish rejection → FAILED, then retry updates the same listing with siblings untouched
- rate limit
- config drift never simulates
- capacity validation and its correction-only status
- one remote listing per local Event
- renderer exactness, address strictness, and HTTP failure classification

All Eventbrite HTTP behaviour runs through the **real** live adapter against an in-memory Eventbrite double on `httpx2.MockTransport`. Automated tests make no network calls.

The four new Playwright flows (create then successor update, uncertain create, exact graphic, config drift) pass on both viewports.

Two Playwright stabilizations were needed:

- The new live spec signs in once per device project, which keeps the suite under the unchanged production login limit of 20 per minute.
- A pre-existing race in the WP1I-A mixed-mode spec (reading execution state before "Start distribution" finished) now waits for the rendered result. It was stable under 16 repetitions.

## Migrations

`0008_resource_bindings`. The foundation migration test passed: empty database to head, `alembic check` (models match migrations), downgrade to base, re-upgrade.

## Files

- **New:**
  - backend: `app/eventbrite.py`, `app/settings_routes.py`, `migrations/versions/0008_destination_resource_bindings.py`, `scripts/eventbrite_smoke.py`, `tests/eventbrite_fake.py`, `tests/test_eventbrite_live.py`, `tests/test_eventbrite_units.py`
  - frontend: `src/settings.tsx`, `src/settings.test.tsx`, `e2e/campaign-eventbrite-live.spec.ts`
  - docs: `docs/eventbrite-live-connector.md` and this report
- **Modified:**
  - backend: `app/config.py`, `app/event_service.py`, `app/execution_routes.py`, `app/execution_service.py`, `app/main.py`, `app/models.py`, `app/simulated_connectors.py`, `tests/browser_server.py`, `tests/test_foundation.py`
  - frontend: `src/App.tsx`, `src/App.module.css`, `src/api.generated.ts` (additive: the Settings route), `src/campaigns.tsx`, `src/campaigns.test.tsx`, `src/events.tsx`, `e2e/campaign-mixed-mode.spec.ts`
  - root: `README.md`, `.env.example`
- No dependencies were added.

## Known limitations

- **Paid tickets** are blocked. The UI has no approved price or currency entry, and Eventbrite payout setup is unverified.
- **Addresses** must be strict US form (`Street, City, ST 12345`); non-US venues are blocked.
- **Graphic display:** the square approved graphic is uploaded unmodified with a full-image crop. How Eventbrite displays it at its listing aspect ratio is outside the app's control.
- **Remote acceptance not yet proven live:** the exact structured-content module shape and media upload-instruction fields follow Eventbrite's documented examples, and are proven against the mock, not against Eventbrite. The optional operator draft write test should be run once with real credentials.
- **Uncertain CREATE with no returned ID** can only be resolved by explicit human disposition. There is no operator "bind this listing" action yet.
- **Bindings are per local Event and account.** Changing the configured organization makes existing approvals block (`EVENTBRITE_ACCOUNT_CHANGED`) and would start a new listing under the new account after fresh approval.
- **No other live connectors**, no OAuth, and no Facebook automation were added. Meetup, LinkedIn, Instagram and Facebook automation remain out of scope pending Architect review.
