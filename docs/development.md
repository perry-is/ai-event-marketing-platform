# Local development (Windows / PowerShell)

Requirements: Python 3.11, Node.js 24, npm, and Docker Desktop. The checked-in `.env.example` contains only harmless local placeholders. Copy it to `.env`; do not put production credentials in the repository.

```powershell
Copy-Item .env.example .env
docker compose up -d --wait
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "backend[test]"
.\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini upgrade head
```

Start the backend in one terminal:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Start the frontend in another terminal:

```powershell
Set-Location frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Without privately configured Google OIDC credentials and an exact allowlist, the app intentionally stops at the sign-in gate. There is no runtime development-login bypass. Real Google sign-in also requires a matching callback URL (`http://localhost:5173/auth/callback`) in the Google client configuration. Production requires HTTPS and non-local database credentials; production configuration fails closed otherwise.

Local Event file originals are stored privately under `var/private/` by default (ignored by Git), outside the frontend's public build directory. Set `EMP_PRIVATE_STORAGE_DIR` only to an absolute private path if you need another local location. `EMP_EXTRACTION_MODE=fake` selects the deterministic offline demo for development; tests use either that demo or a mocked OpenAI HTTP boundary. Production requires `EMP_EXTRACTION_MODE=openai`, `EMP_CAMPAIGN_GENERATION_MODE=openai`, the server-only `OPENAI_API_KEY`, `OPENAI_EXTRACTION_MODEL` (default `gpt-6-luna`), and `OPENAI_CAMPAIGN_MODEL` (configured in `.env.example` as `gpt-6-luna`; select an account-enabled Luna-class model). Local fake mode is the default. To opt into a live local campaign call, set `EMP_CAMPAIGN_GENERATION_MODE=openai`, privately configure the key and model, and keep those credentials out of source control. Campaign generation sends validated Event facts; revision also sends current campaign copy and the revision instruction. It does not send source files, use tools/web search, or publish. Requests use strict structured output, disable response storage, and cap output tokens. See [AI extraction boundaries](docs/ai-extraction.md) and [AI campaign generation](docs/ai-campaign-generation.md).

Event intake defaults to `America/Denver` (shown as Mountain Time) and can be changed with `EMP_DEFAULT_TIMEZONE`. The Example Conference Center address is a configurable suggestion (`EMP_DEFAULT_VENUE_*`); it is not saved to an Event until accepted. Description suggestions use the configured campaign model/mode and remain proposals until the operator explicitly saves them. No destination mode is changed by these intake settings.

Graphic generation is also fake/offline by default. Production requires `EMP_IMAGE_GENERATION_MODE=openai` with the same server-only `OPENAI_API_KEY`; `OPENAI_IMAGE_MODEL` selects the Images API model and defaults to `gpt-image-2.5-flare`. A live local graphic call can be enabled with `EMP_IMAGE_GENERATION_MODE=openai` after privately configuring an account-enabled model and key. The image adapter passes Event topic/family/delivery context plus bounded campaign creative direction and the optional visual revision instruction; an edit also sends the prior generated visual layer. It does not select source material or supply the canonical Event title/schedule/location as separate fields, though preference text may itself mention Event details. Authoritative Event text is composed locally into a private square graphic. See [campaign graphics](docs/campaign-graphics.md).

Facebook Group recommendations need the Group Master imported once into the application database. The importer reads the local `.xlsx` copy without modifying it, stores a private copy, and is safe to repeat (identical bytes are a no-op):

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m app.group_catalog "C:\path\to\group-master.xlsx"
```

Ranking is fake/deterministic by default. Production requires `EMP_GROUP_RECOMMENDATION_MODE=openai` with the server-only `OPENAI_API_KEY`; `OPENAI_GROUP_RECOMMENDATION_MODEL` selects the Luna-class model (default `gpt-6-luna`). If ranking is unavailable, the app falls back to deterministic suggestions and manual browsing. Only a bounded, pre-filtered candidate list plus accepted Event facts and the campaign summary are sent. See [Facebook Group recommendations](docs/facebook-group-recommendations.md).

Run the checks from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests
.\.venv\Scripts\ruff.exe check backend
Set-Location frontend
npm run format:check
npm run lint
npm test
npm run types
npm run build
npm run test:e2e
npm audit --audit-level=moderate
```

The browser suite uses isolated test-only identity and mocked provider HTTP for extraction, campaign generation, and live connector paths, with disposable PostgreSQL databases; it does not enable a local login bypass or contact AI/provider APIs. Playwright Chromium is required (`npx playwright install chromium`). It exercises protected access, Event intake/clarification, campaign generation/revision, exact-version approval, independent destination outcomes/retries/reconciliation/manual completion, immutable history, stale-Event handling, and desktop/mobile layout without live provider accounts. Destination mode is server-owned: Eventbrite, Meetup, LinkedIn organization and Instagram default to SIMULATED; Facebook Groups stays MANUAL. Mocked tests exercise Eventbrite, Meetup, LinkedIn Organization and Instagram LIVE adapters without provider network calls. Live Google sign-in requires operator-provisioned OIDC credentials and allowlist entry.

For local or deployment configuration, `EMP_EVENTBRITE_EXECUTION_MODE`, `EMP_MEETUP_EXECUTION_MODE`, `EMP_LINKEDIN_ORGANIZATION_EXECUTION_MODE` and `EMP_INSTAGRAM_EXECUTION_MODE` accept only `SIMULATED` or `LIVE`. Keep these server-side settings out of browser configuration. A configured mode does not itself provide a connector: unsupported LIVE entries are blocked rather than silently simulated. Current local defaults are listed in `.env.example`.

With `EMP_EVENTBRITE_EXECUTION_MODE=LIVE`, configure the server-only `EVENTBRITE_PRIVATE_TOKEN`, `EVENTBRITE_ORGANIZATION_ID`, `EVENTBRITE_ORGANIZER_ID` and `EVENTBRITE_CURRENCY`; missing values block live approval and are shown on the Settings page (the token is never displayed or stored in business data). New approval plans freeze Eventbrite LIVE with that account; existing approvals are never re-moded by later configuration. Check access read-only with:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m scripts.eventbrite_smoke
```

The smoke check never writes unless `EVENTBRITE_ENABLE_LIVE_WRITE_TEST=1` is set, and even then only creates, verifies and deletes a synthetic unlisted draft. Automated tests mock the Eventbrite HTTP boundary and never contact Eventbrite. See [live Eventbrite connector](docs/eventbrite-live-connector.md).

Meetup LIVE is configured separately with `EMP_MEETUP_EXECUTION_MODE=LIVE`, `MEETUP_CLIENT_KEY`, `MEETUP_AUTHORIZED_MEMBER_ID`, `MEETUP_SIGNING_KEY_ID`, `MEETUP_PRIVATE_KEY_PATH`, `MEETUP_NETWORK_URLNAME` and `MEETUP_GROUP_URLNAME`. The RSA key must be mounted outside the repository and the configured member must own the Meetup OAuth client. New approvals freeze the exact target Group/account; config drift blocks old approvals. Read-only readiness can be checked with `python -m scripts.meetup_smoke` from `backend/`. The synthetic draft test is disabled unless both `MEETUP_ENABLE_LIVE_WRITE_TEST=1` and `MEETUP_ENABLE_VERIFIED_DRAFT_CLEANUP=1` are set; run it separately with `python -m scripts.meetup_draft_write_test`. It creates, verifies and deletes a future synthetic draft and never publishes. Meetup photo upload is deferred: graphic-bearing campaign versions block before any Meetup mutation until the binary upload contract is verified. The connector currently supports free, in-person Events only and requires one exact matching Group venue. See [live Meetup connector](docs/meetup-live-connector.md).

LinkedIn Organization LIVE uses the current versioned Posts and Images APIs and is configured with `EMP_LINKEDIN_ORGANIZATION_EXECUTION_MODE`, `EMP_LINKEDIN_API_VERSION` (currently `202609`), and the server-side `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI`, `LINKEDIN_ORGANIZATION_ID`, and `LINKEDIN_COMMUNITY_ACCESS_TIER`. Authenticated users can connect/reconnect a LinkedIn member account from Settings; the delegated token is encrypted in PostgreSQL using the external credential keyring. A client ID, connected member, or configured tier alone does not prove Community Management approval or Page access. The read-only smoke command reports `DEFERRED` when no stored OAuth connection exists; when configured it performs read-only provider checks. Never place secrets in browser configuration. See [LinkedIn Organization connector](docs/linkedin-organization-connector.md), [OAuth connections](docs/oauth-connections.md), and [credential vault](docs/credential-vault.md).

Instagram LIVE uses the Instagram Login API (`graph.instagram.com`) with API version `v26.0` as of 2026-10-07. New approvals freeze the configured Professional account ID; the username is display-only. Required permissions are `instagram_business_basic` and `instagram_business_content_publish`. Set `EMP_INSTAGRAM_EXECUTION_MODE=LIVE`, `INSTAGRAM_CLIENT_ID`, `INSTAGRAM_CLIENT_SECRET`, `INSTAGRAM_REDIRECT_URI`, `EMP_INSTAGRAM_PROFESSIONAL_ACCOUNT_ID`, `EMP_INSTAGRAM_USERNAME`, `PUBLIC_MEDIA_BASE_URL` (public HTTPS), and `MEDIA_DELIVERY_SIGNING_SECRET` (deployment secret, at least 32 characters) only when operating the live connector. Authenticated users can connect/reconnect the Professional account from Settings. Tokens are encrypted with the external keyring, but app review/access and real account readiness remain unverified. The read-only smoke command reports `DEFERRED` if no stored connection exists and otherwise performs read-only provider checks: `python -m scripts.instagram_smoke` from `backend/`. Automated tests exercise the real LIVE adapter through a fake provider; none publish to Instagram. See [Instagram connector](docs/instagram-live-connector.md), [OAuth connections](docs/oauth-connections.md), and [credential vault](docs/credential-vault.md).

For local vault setup, choose an absolute keyring path outside the repository, set `CREDENTIAL_VAULT_KEYRING_PATH`, then run `python -m scripts.credential_vault generate-key --key-id local-2026-10` from `backend/` and set `CREDENTIAL_VAULT_ACTIVE_KEY_ID=local-2026-10`. The command creates a random 256-bit key in that external file and never prints the key. If these settings are absent, provider connection stays unavailable and the rest of local development remains usable. Never copy the keyring into Git, PostgreSQL, a container image, or a database-only backup.

The backend exposes `/health/live` for process liveness and `/health/ready` for database/migration readiness. Authenticated mutations must use the server-issued CSRF token and same-origin request checks. The Vite development server proxies API/auth calls to `http://127.0.0.1:8000`.

`.gitignore` excludes local credentials, environments, dependencies, databases, uploads, caches, and editor noise; review status before committing.
