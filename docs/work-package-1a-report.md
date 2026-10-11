# Work Package 1A — Protected Application Foundation

**WP1A COMPLETE / ACCEPTED.** PostgreSQL-backed migrations, the complete backend suite, live application readiness, and desktop/mobile Playwright acceptance all pass. Live Google OIDC login remains intentionally deferred because provider credentials are not configured; the mocked OIDC boundary passes.

## Implementation

The backend is a small FastAPI application with settings validation, SQLAlchemy/PostgreSQL access, Alembic migrations, health endpoints, Google OIDC login, server-side sessions, and a protected `/api/me` endpoint. The only persisted entities are the authorized application identity and its server-side session; no Event or other product workflow tables were added. The first migration creates those two tables and has been exercised against an empty PostgreSQL database by the backend tests.

The React 19 / TypeScript / Vite shell contains a public sign-in gate and protected New Event, Events, Groups, and Settings placeholders. It has a responsive desktop rail/mobile navigation, textual branding, and no invented metrics or business behavior. Browser authorization is always rechecked through the backend; it is not inferred from frontend state.

Google OIDC uses an authorization code, one-use state/nonce, PKCE, validated signed identity claims, and an exact email allowlist for first-time account binding. Subsequent authorization uses the bound issuer/subject. Provider access and refresh tokens are not retained or returned. Sessions use random opaque browser identifiers with only a SHA-256 digest stored in PostgreSQL, idle and absolute expiration, revocation, and rotation after successful login. Cookies are host-only, HttpOnly, SameSite=Lax, and Secure in HTTPS production. Production settings require HTTPS, OIDC credentials, a nonempty allowlist, and non-local database credentials; runtime auth bypass configuration is rejected. No password login, roles, public registration, or platform OAuth was introduced.

Cookie-authenticated mutations require the authenticated server-issued CSRF token and an exact same-origin `Origin` check. The only state-changing route in scope is logout. Callback errors and requests do not log raw credentials, authorization codes, tokens, or session identifiers; Uvicorn access logging is disabled because callback query strings contain authorization codes. Liveness is separate from database/migration readiness.

PostgreSQL runs as the only Compose service, bound to loopback. SQLAlchemy, Alembic, Authlib, Pydantic Settings, and psycopg support the selected backend boundaries. React, React Router, Vite, TypeScript, Vitest, Testing Library, ESLint, Prettier, and Playwright support the browser shell and its checks. Versions are pinned in the backend project and frontend lockfile. OpenAPI-derived frontend types can be regenerated from the local application without a checked-in temporary schema file.

## Configuration and local workflow

`/.env.example` contains only local placeholders. The ignored local `.env` was not added to Git. Local steps for PostgreSQL, Python environment setup, migrations, backend/frontend start, test commands, and optional private live Google OIDC configuration are in the root README. There is no runtime local sign-in bypass. The browser suite substitutes the OIDC boundary only in a test-configured harness and creates disposable PostgreSQL databases.

## Acceptance results

- **PostgreSQL:** Docker Desktop 29.7.2 was reachable; Compose PostgreSQL 17.9 reported healthy on the project's loopback port 5433.
- **Empty-database migration:** the migration test created an empty disposable PostgreSQL database, upgraded to head, confirmed the expected auth/session tables, checked migration consistency, downgraded, and upgraded again: **1 passed**. The normal local development database also upgraded successfully and reported Alembic revision `0001_auth_sessions (head)`.
- **Application readiness:** started the regular FastAPI development app against the configured PostgreSQL database. `/health/live` returned 200/alive and `/health/ready` returned 200/ready. With local Google credentials absent, `/auth/status` correctly reported login unavailable; unauthenticated `/api/me` returned 401.
- **Full backend suite:** `pytest -q backend/tests`: **45 passed**, 0 failed, 0 errors. This includes mocked OIDC validation, allowlist/session behavior, expiry/revocation, logout/CSRF, and production fail-closed configuration.
- **Mocked OIDC boundary:** **PASS.** Browser acceptance used the explicit test-only provider fixture; backend OIDC tests validate provider responses without contacting Google.
- **Live Google OIDC:** **DEFERRED.** No provider credentials/account configuration was supplied, as expected for this package.
- **Browser acceptance:** Playwright: **2 passed**, 0 failed. The desktop and iPhone 13 Chromium projects each verified unauthenticated navigation to login, unauthorized direct API rejection, test sign-in, protected shell/navigation access, logout, session revocation (including reuse of the old cookie), and no horizontal overflow at the target viewport. Test-only API sessions exposed only display name and CSRF token. Readiness was separately verified against the live PostgreSQL-backed development server.
- **Visual review:** reviewed the captured desktop and mobile shell screenshots. Navigation, sign-out, headings, and placeholder content were visible and fit their respective layouts; no horizontal overflow was detected.
- **Frontend unit tests:** **7 passed**. Frontend formatting, lint/type checking, generated API types, and production build passed.
- **Python formatting/lint:** `ruff format --check backend` and `ruff check backend` passed.
- **Dependency audit:** `npm audit --audit-level=moderate`: **0 vulnerabilities**.
- **Source integrity:** validator passed; the four supplied downloads and six root JPEG hashes remain unchanged, and source audit counts reconcile.
- **Git/security checks:** `git diff --check` passed; local `.env`, virtual environment, dependency folders and test artifacts remain ignored; the checkpoint secret scan found no likely secrets.

## Scope boundary and outstanding architecture conflict

The Phase 1 proposal described a P1-A authorized Event draft that survives reload. This WP1A request explicitly prohibits the Event workflow and Event persistence. Following the package-specific boundary, no Event workflow/table was added; implement that draft in the later event-intake package after Architect review.

No ORM product model, frontend business screen, AI call, file upload, campaign generation/versioning, approval, group recommendation, connector, platform OAuth, simulated or real publishing, deployment, or migration beyond user/session foundation is included.

## Files

The provisional implementation commit contains the `backend/` FastAPI, auth, settings, database, migration and test files; the `frontend/` React/Vite shell, generated API types, unit/browser test files, and npm manifests/lockfile; root `compose.yaml`, `.env.example`, `.gitignore`, and README; and the narrow `scripts/verify-source-audit.py` adjustment that keeps the Phase 0 placeholder assertion available only when explicitly requested. `backend/tests/test_config.py` adds database-independent tests for fail-closed configuration. This acceptance commit changes only this report. No original source/reference artifact was changed, and no credentials or live provider tokens are included.

**Git checkpoint:** implementation remains preserved in provisional commit `a4f8bbe64443353189350d62bd3f4996c92bf435`. This acceptance update changes documentation only and is committed separately as `docs: accept WP1A application foundation`. No application behavior changed during acceptance recovery.
