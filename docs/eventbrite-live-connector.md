# Live Eventbrite connector

WP1I adds the first real live destination connector, built on the [mixed-mode approval model](mixed-mode-approval.md). Eventbrite LIVE is one entry in an immutable approved plan. Meetup, LinkedIn organization and Instagram stay SIMULATED, and Facebook Groups stays MANUAL. There is no global runtime mode, and the browser never chooses LIVE or SIMULATED.

## Authentication: V1 private token

- The server reads a private token from `EVENTBRITE_PRIVATE_TOKEN` and sends it only as a `Bearer` header to `https://www.eventbriteapi.com/v3`.
- The token is never:
  - persisted in the database
  - written to a plan, approval, execution or attempt
  - returned by any API
  - logged
  - sent to the media-upload storage URL
- Credential lookup is isolated behind `CredentialProvider` (`PrivateTokenCredentials`). OAuth is **deferred**. It can replace that provider without changing rendering, create/update, resource binding, publish, retry or reconciliation.

## Configuration

| Setting | Purpose |
| --- | --- |
| `EMP_EVENTBRITE_EXECUTION_MODE` | `SIMULATED` (default) or `LIVE`. Used only to build **new** proposed plans. |
| `EVENTBRITE_PRIVATE_TOKEN` | Server-only credential. |
| `EVENTBRITE_ORGANIZATION_ID` | Numeric organization that owns created events and venues. Never guessed. |
| `EVENTBRITE_ORGANIZER_ID` | Existing numeric organizer attached to each event. No organizer is created per Event. |
| `EVENTBRITE_CURRENCY` | Account currency, a required Eventbrite event field. It is not a price. |

When the mode is LIVE and any value is missing, the LIVE entry is BLOCKED with a specific reason before approval. The connector never falls back to SIMULATED.

## Readiness

There are two levels.

- **Local (approval preview, start, retry):**
  - configuration is complete
  - the approved account matches the configured account
  - the deterministic renderer accepts the exact approved entry

  These checks need no network, as the WP1I-A readiness rule requires.
- **Live read-only (Settings page and the operator smoke check):** `GET /users/me/`, then the configured organization in `GET /users/me/organizations/`, then the configured organizer in `GET /organizations/{id}/organizers/`. The result is `READY`, `NEEDS_SETUP` or `UNAVAILABLE`, with human reasons and the organization and organizer names. No token, IDs, headers or provider payloads reach the browser.

## Mixed-mode plan integration

When the server mode is LIVE and the account is configured, the Eventbrite plan entry gets:

- `runtime_mode: LIVE`
- the stable target `eventbrite-organization:{org}`
- an `eventbrite` block with the approved account (organization, organizer, currency)
- the exact listing facts frozen from the CampaignVersion snapshot: name, start/end instants, IANA timezone, delivery, venue name, address, price kind, amount/currency (if any) and capacity

These are hashed into the destination-plan hash, not the CampaignVersion hash. A mode or account change therefore changes only the plan hash and needs a fresh approval.

## Execution uses the stored plan

- **Connector choice:** the registry selects the connector from the approved entry (`EVENTBRITE`, `AUTO`, `LIVE`). Current configuration only decides whether that exact connector is available.
- **Config drift:** an approval frozen as LIVE whose server is now SIMULATED is BLOCKED (`APPROVED_CONNECTOR_MODE_UNAVAILABLE`). It is never executed by the simulator. A changed configured account also blocks (`EVENTBRITE_ACCOUNT_CHANGED`).
- **Checks before every live operation:** the existing claim protocol verifies:
  - the approval exists
  - the exact CampaignVersion and content hash
  - the stored plan hash
  - active authority
  - material Event consistency
  - the plan entry and its LIVE mode
  - connector readiness

  The adapter then re-renders only from the approved entry. The latest unapproved version is never used. A successor version withdraws the earlier approval through the existing successor rules, so an old approval cannot publish after a newer version exists.

## Deterministic renderer

No AI is involved, and no copy is rewritten or truncated. A hard constraint violation blocks with a specific reason:

| Requirement | Rule |
| --- | --- |
| Name | Exact approved Event title, HTML-escaped. |
| Schedule | Start and end converted to UTC, with the approved IANA timezone. A missing end blocks; no duration is invented. |
| In-person / hybrid | Needs a venue name and a strict US street address of the form `Street, City, ST 12345` (country US is derived only from a valid US state code). Anything else blocks. |
| Virtual | `online_event: true`, and no venue is created. |
| Free tickets | One `General Admission` free ticket class whose `quantity_total` equals the approved Event capacity. |
| Capacity | Eventbrite requires `quantity_total`. A missing capacity blocks (`EVENTBRITE_CAPACITY_REQUIRED`); no number is ever invented. Capacity is a new correction-only Event fact. AI extraction cannot set it. |
| Paid tickets | Blocked (`EVENTBRITE_PAID_TICKETS_UNSUPPORTED`). There is no approved price entry or verified payout setup, and paid is never turned into free. |
| Unknown price kind | Blocked. |
| Description | The exact approved Eventbrite text, HTML-escaped. Blank lines become paragraphs and line breaks become `<br>`. It is sent as one structured-content text module (`purpose: listing`, `publish: true`), the minimum stable V1 representation. |

## Stable external resource binding

The `destination_resource_bindings` table (migration `0008_resource_bindings`) holds one row per (destination, account, local **Event**). It records:

- the Eventbrite event ID, then the verified URL
- state (`DRAFT` or `PUBLISHED`)
- reusable resource metadata: venue ID and fingerprint, ticket class ID, logo media ID and graphic hash
- the binding and last executions, first bound time and last verified time

Two unique constraints apply:

- one remote listing per local Event per account
- one local Event per remote listing, so no other Event can claim it

A trigger makes the identity columns immutable and forbids deletion. No OAuth or token storage exists.

The binding is per Event, not per CampaignVersion or Approval, because an Eventbrite listing represents the occurrence itself. Successor approved versions reuse it.

## Create versus update

| Situation | Action |
| --- | --- |
| No verified binding | CREATE: `POST /organizations/{org}/events/` |
| Binding exists | UPDATE: `POST /events/{id}/` |

The rule is deterministic. The browser, AI and approval request cannot choose it, and the external ID is never accepted from the client.

After a successful CREATE, the new ID is bound in its own committed transaction **before** any further remote step. Ticket classes, logo and venue are reused through the binding metadata on later updates.

## Draft-first operation order

1. Preflight: render plus readiness.
2. Venue (in-person/hybrid only).
3. CREATE as an unpublished draft (with `listed: true`) or UPDATE.
4. Ticket class: create, or update the bound one.
5. Structured-content description.
6. Approved logo.
7. Read back.
8. Publish, only if not already live.
9. Verification read.

Publish happens only after every dependent step succeeds, so a partially configured event is never published. Eventbrite's own publish validation failures are reported honestly as FAILED. A remote draft may then exist, bound, and a retry updates it.

## Venue

Reuse happens only on a **strong match**: identical normalized venue name, street, city, state and postal code, either from the binding fingerprint or the organization's existing venues. A matching name with a different address never reuses; a new venue is created instead.

## Approved graphic

If the approved plan entry carries a graphic, the adapter loads the exact asset bytes for that approved CampaignVersion's manifest. It checks the asset ID and SHA-256 and re-validates the bytes. The upload sequence is:

1. Request upload instructions (`GET /media/upload/?type=image-event-logo`).
2. Upload the file to the provider-issued HTTPS URL **without** the Eventbrite credential.
3. Register it with a full-image crop mask.
4. Set `logo_id`.

A newer unapproved graphic is never used. Campaigns without a graphic proceed without one.

## Verification and success

SUCCESS requires a verification read showing all of:

- the expected bound ID
- the approved name, start and end
- status `live` or `started`
- an `https://www.eventbrite.*` URL

A 200 response alone is never success. The verified URL and time are stored on the binding and the execution, and the UI shows a **View on Eventbrite** link only for verified live results.

## Idempotency, retries and UNKNOWN

- The WP1I-A execution identity and claim rules are unchanged:
  - SUCCEEDED and RUNNING are never re-dispatched.
  - UNKNOWN rejects retry and requires reconciliation.
  - Retry acts on Eventbrite only, with the same Approval, CampaignVersion, LIVE mode and binding.
- **Failure classification:**

  | Category | Result |
  | --- | --- |
  | 401 | `AUTHENTICATION` |
  | 403 | `PERMISSION` |
  | 404 | `NOT_FOUND` |
  | 429 | `RATE_LIMIT` |
  | Other 4xx | `VALIDATION` |
  | 5xx on a write | `REMOTE_SERVER` with a possibly applied effect |
  | Connect failure | `NETWORK`, not sent |
  | Read/write timeout or protocol error on a write | Possibly applied |

  Stable Eventbrite error codes are kept as safe diagnostics; raw payloads are never shown.
- **Uncertain CREATE** (timeout after a possible acceptance, a 5xx, an unparseable success, or a failure to bind) becomes **UNKNOWN**: "Eventbrite may have created the event, but the result could not be verified."
  - Ordinary retry is unavailable.
  - Reconciliation never searches by title or date. With no known ID it stays UNKNOWN for explicit human disposition (the existing acknowledge-uncertainty path). A successor approval targeting the same account is then blocked or requires a duplicate-risk acknowledgement through existing rules.
- **Reconciliation with a known ID:** it reads `GET /events/{id}/`.
  - FOUND_MATCHING: the approved state is live.
  - CONFIRMED_ABSENT: the bound listing exists but isn't in the approved published state. The retry is an UPDATE by ID and can never create.
  - INCONCLUSIVE: otherwise.
- **Retryable failures:** rate limits, unsent network failures, remote server errors and auth/permission failures are explicitly retryable. There are no automatic retries. With a bound listing, provider validation failures are also retryable, because a retry can only update it.

## Mixed-mode UI

Results are labelled truthfully:

- Eventbrite: "Published · Live on Eventbrite · verified", with the link.
- Simulated siblings: "Published (Simulated) · Practice mode".
- Facebook Groups: its manual status.

A drift-blocked live entry says it was approved to publish live, that nothing was sent, and that it was not switched to practice mode. Readiness blockers on approval preview use plain language and point to Event details or Settings. Settings shows the Eventbrite status, organization and organizer names and blockers, without credentials.

## Smoke and write tests

- `python -m scripts.eventbrite_smoke` (from `backend`) is **read-only** by default. It reports `DEFERRED` when credentials are absent.
- The optional live write test runs only with `EVENTBRITE_ENABLE_LIVE_WRITE_TEST=1`. It creates a clearly synthetic, **unlisted online draft**, verifies it, and deletes it. It never publishes.
- Normal automated tests never contact Eventbrite. They drive the **real** live adapter through a stateful in-memory Eventbrite double on an `httpx2.MockTransport`.

## Security summary

- The token stays server-side and the browser cannot choose mode, CREATE/UPDATE or external IDs.
- Every route is authenticated. Mutations need CSRF.
- Test-only mode and fake routes exist only in `environment=test`, and transport substitution is refused outside tests.
- Logs carry lifecycle events and normalized categories, never Authorization headers or full payloads.
- No other live connector, no OAuth, and no Facebook automation were added.

## Verification of API behaviour

These were checked against Eventbrite's official v3 reference and published guides during implementation:

- endpoint paths and the publish response shape (`{"published": true}`)
- that a ticket class requires `quantity_total`
- the structured-content version, purpose and publish model
- the media upload-token flow

The official documentation pages render client-side, so some field-level details came from Eventbrite's documented examples and should be confirmed by the optional live draft test. Those details are the exact text-module shape and whether upload-instruction fields are optional. Mocked tests prove the application's safety rules, not Eventbrite's acceptance of every payload.
