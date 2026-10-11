# Live Meetup connector

WP1J adds a server-to-server Meetup connector for one approved Event Marketing Meetup Group. New plans still default to `SIMULATED`; the server setting `EMP_MEETUP_EXECUTION_MODE=LIVE` applies only to newly proposed plans. An Approval freezes the exact Meetup runtime mode, Group URL name, Pro Network URL name, and a non-reversible fingerprint of the authorized member account. Execution, verification, and retry use that stored plan. If current configuration cannot honor the approved mode, account, or Group, the old Approval is blocked; it is never silently redirected or simulated.

## Authentication and readiness

V1 uses Meetup's JWT owner flow. The server signs a short-lived RS256 assertion with the configured key and exchanges it at Meetup's token endpoint. Claims are `iss` (OAuth client key), `sub` (authorized member ID), `aud=api.meetup.com`, short `iat`/`exp`, and header `kid` (signing-key ID). The returned access token is cached in memory only. The RSA private key is mounted outside the repository; neither assertion, access token, key, nor raw provider response is returned or logged. Meetup documents that JWT access is limited to the member who owns the OAuth client, so readiness compares `self.id` to `MEETUP_AUTHORIZED_MEMBER_ID` before allowing live work. Interactive OAuth Server Flow is deferred.

Configure privately:

| Setting | Purpose |
| --- | --- |
| `EMP_MEETUP_EXECUTION_MODE` | `SIMULATED` by default; `LIVE` is explicit server configuration. |
| `MEETUP_CLIENT_KEY` | Meetup OAuth client key / JWT issuer. |
| `MEETUP_AUTHORIZED_MEMBER_ID` | The Meetup member expected to own the client. |
| `MEETUP_SIGNING_KEY_ID` | Meetup-registered key identifier used in JWT `kid`. |
| `MEETUP_PRIVATE_KEY_PATH` | Absolute path to the private RSA key outside this repository. |
| `MEETUP_NETWORK_URLNAME` | Explicit Pro Network target checked during readiness. |
| `MEETUP_GROUP_URLNAME` | One explicit Group target frozen into new approvals. |

The authenticated `self`, Pro Network, and Group are read before live operations. Readiness requires the returned member ID to match configuration, the configured Network and Group to exist and be active, and the Group's `proNetwork.urlname` to match. A token alone is not readiness. Settings reports account/Group readiness separately from the campaign-specific photo limitation. This implementation does not assert that the operator owns a configured client until real account credentials are deliberately provided and checked.

The current Meetup GraphQL endpoint is `https://api.meetup.com/gql-ext`; named operations and variables are kept in `backend/app/meetup.py`. User values are always GraphQL variables. HTTP errors and GraphQL `errors` are both evaluated; HTTP 200 with top-level or mutation-payload errors is never treated as success. Rate-limit errors map to the existing retryable category. No raw error body is shown to the operator.

## Approved content, validation, and current safe subset

The renderer accepts only the exact approved CampaignVersion plan entry and makes mechanical API values. It does not use AI, change Event facts, rewrite content, or choose targets. Approved Meetup copy is escaped and wrapped in deterministic simple HTML paragraphs. Event title is limited to the documented 80 characters and description to 50,000 characters. Start time is converted from the frozen Event instant into its approved timezone offset. The Event's known end time determines duration; no default is invented.

Official schema introspection distinguishes the inputs: `CreateEventInput.duration` is ISO-8601 (for example, `PT1H30M`); `EditEventInput.duration` is milliseconds. The adapter renders both deterministically. Creation explicitly sets `publishStatus: DRAFT` and `selfRsvp: false`, avoiding Meetup's documented default of automatically RSVPing the authenticated member. This schema check also confirmed `venueId`, `groupUrlname`, `startDateTime`, and the draft/published status fields used by the named operations.

The initial implementation intentionally supports only **free, in-person Events**. Paid Events are blocked rather than turned into free Events. Virtual and hybrid Events are blocked because their location/type inputs need a separately reviewed exact mapping. In-person publication requires exactly one Group venue matching the approved venue name, street, city, state, and country after conservative case/punctuation normalization. It does not create venues or fuzzy-match them. Missing or ambiguous venue data blocks before any mutation. Meetup's event does not receive a made-up capacity, payment configuration, or meeting URL.

## Draft, update, publish, and verification

For an Event with no existing resource binding, execution verifies the approved mode/version/plan, member, Network, Group, and venue, then creates one draft. It immediately stores the returned ID in the generic `DestinationResourceBinding` keyed by local Event, Meetup destination, and account fingerprint. A later approved CampaignVersion updates that same Meetup Event; title matching is never used to discover identity. If an existing binding belongs to another Group, the connector blocks instead of moving or duplicating the event.

After create or update, the connector reads the exact external ID and verifies title, deterministic description, start instant, duration, physical event type, Group, and matched venue. A draft is published with `editEvent` only after the draft readback matches. The connector then reads the same ID again. `SUCCESS` requires the exact title, date/time, duration, description, Group, venue, active/published state, and a verified Meetup event URL. A mutation response alone is not success.

Updates use the same bound ID and do not make a second Event. A successor CampaignVersion must have its own Approval. A changed Group target for an already-bound local Event is blocked. A changed server mode or owner also blocks execution of an old plan. The connector never alters an approved CampaignVersion, graphic, destination, or group decision.

## Graphic limitation

Meetup's [official GraphQL guide](https://www.meetup.com/graphql/guide/) documents that `createGroupEventPhoto` returns an `uploadUrl` and that binary upload is a separate second step. The public guide does **not** specify the second-step HTTP method, body encoding, or required request headers. WP1J therefore does not guess POST, PUT, multipart, raw bytes, or provider-specific headers, and production code does not create a photo placeholder.

* If the exact CampaignVersion has **no approved graphic**, Meetup LIVE may proceed if all other requirements are ready.
* If it **has an approved graphic**, Meetup LIVE is blocked with `MEETUP_GRAPHIC_UPLOAD_UNVERIFIED` before any Meetup mutation. The app never silently omits the approved graphic.

This campaign-specific blocker does not mark the Meetup account connector broken. Settings can truthfully report the account and Group as connected while separately showing “Live image upload awaiting verification.” The photo branch is reserved for WP1J-P, after authoritative transport documentation or an explicitly authorized synthetic-account verification.

## Idempotency, failure, and reconciliation

The generic `DestinationExecution` is stable per approved plan target. A successful execution does not repeat; an in-progress execution is not dispatched a second time; a confirmed retryable failure can be explicitly retried. Retries keep the exact Approval mode, version, account and Group target and update a known remote ID rather than creating another Event.

A timeout or ambiguous response after a mutation may have reached Meetup becomes `UNKNOWN`; ordinary retry is unavailable. When an external ID is known, Verify queries that exact ID and can reconcile it. Without an ID, the connector will not search or bind by title/date. It stays uncertain for operator handling. GraphQL domain validation/rate-limit errors are handled distinctly when they establish that no change occurred; ambiguous server/network results are not converted into success.

Meetup documents a points-based rate limit (500 points per 60 seconds); the connector performs no aggressive automatic retry loop. A confirmed rate-limit response is categorized as retryable through the existing explicit retry path. No announcements, messages, RSVP operations, network-wide events, or cross-group publication are implemented.

## Smoke tools and side-effect controls

Read-only authentication, member, Network, and Group checks:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m scripts.meetup_smoke
```

This does not write. With absent configuration it reports `DEFERRED`; it never prints tokens, assertions, or key material.

A separate future-draft tool is gated by **both** `MEETUP_ENABLE_LIVE_WRITE_TEST=1` and `MEETUP_ENABLE_VERIFIED_DRAFT_CLEANUP=1`. It creates a clearly synthetic future DRAFT, reads it back, calls the documented delete operation only after the operator has deliberately attested cleanup behavior is verified, and verifies deletion. It never publishes or uploads a photo. If cleanup fails, it prints the external Event ID for manual removal. The flags are off by default, and this package does not run the write test against a real account.

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m scripts.meetup_draft_write_test
```

Automated backend and Playwright tests use a stateful fake HTTP transport while exercising the actual LIVE adapter. They make zero Meetup network calls. The only external check during implementation was anonymous, read-only GraphQL schema introspection against Meetup's official endpoint to verify operation/input/output fields and the documented create/edit duration distinction.

## Official references

* [Meetup GraphQL guide](https://www.meetup.com/graphql/guide/) — endpoint, GraphQL operations, draft/publish behavior, photo placeholder and upload-URL limitation, rate limit.
* [Meetup GraphQL authentication](https://www.meetup.com/graphql/authentication/) — JWT owner authentication and claims.
* [Meetup API authentication options](https://help.meetup.com/hc/en-us/articles/41454853218317-Which-API-authentication-should-I-choose) — JWT access is limited to the member who owns the OAuth client.

Schema descriptions were checked through anonymous read-only introspection on 2026-10-07. Account ownership/access remains **UNVERIFIED** until a read-only smoke is run with deliberately configured Event Marketing credentials.
