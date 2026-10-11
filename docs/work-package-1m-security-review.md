# WP1M security review: credential vault and OAuth connections

Independent adversarial review of the implemented WP1M credential vault, done before any real LinkedIn or Instagram credentials are stored. No real provider account was connected and no LinkedIn or Meta request was made. Every conclusion is based on the code, not on the WP1M report's claims.

## 1. Baseline reviewed

- HEAD `e1cee1f911fb022952a05326f878a0e3e880f54f` (`feat: add secure credential vault and OAuth connections`), clean working tree, source integrity PASS.
- The reported validation was re-run rather than trusted. The backend, frontend and Playwright counts reproduced. However, **`ruff check` and `ruff format --check` failed at baseline** on `migrations/versions/0009_credential_vault.py`, so the report's "lint/format passed" claim was inaccurate (finding L5).

## 2. Files and components reviewed

- **Vault:** `app/credential_vault.py`, the vault models in `app/models.py`, and migration `0009_credential_vault`.
- **OAuth:** `app/oauth_connections.py` (gateway, state, callbacks, reconnect, refresh, disconnect).
- **Connectors:** the credential providers and account checks in `app/linkedin.py` and `app/instagram.py`.
- **App wiring:** `app/settings_routes.py`, `app/main.py` and `app/config.py`.
- **Tooling and tests:** `scripts/credential_vault.py`, the smoke scripts, `tests/test_oauth_vault.py` and `tests/browser_server.py`.
- **Frontend and docs:** `frontend/src/settings.tsx`, `docs/credential-vault.md`, `docs/oauth-connections.md` and `.env.example`.

## 3. Threat model assessment

The vault correctly targets five threats:

- a database dump or backup
- casual DB inspection
- API serialization
- application logs
- Git and frontend exposure

It explicitly does not defend against code execution in the running app, host compromise, or an attacker holding both the DB and the keyring. That boundary is appropriate.

The primary question was whether an attacker can obtain or misuse provider credentials through a path WP1M was meant to prevent. **No exploitable path was found for an attacker without both DB access and the keyring.** Two misconfiguration and operational weaknesses were found and fixed (M1, M2).

## 4. Cryptographic implementation

- Uses `cryptography`'s supported high-level `AESGCM` AEAD (`cryptography==50.0.2`, pinned). There is no custom cryptography.
- Keys must be exactly 32 bytes (AES-256). This is checked at keyring load and again before every encrypt and decrypt.
- Ciphertext includes the GCM tag (AESGCM output). `decrypt` authenticates before any plaintext is returned, and `InvalidTag` maps to a generic `CredentialVaultError`.
- There is no plaintext fallback: an unreadable envelope makes the credential unavailable. Encryption is not deterministic, because every encrypt draws a fresh nonce.
- The payload is bounded (32 KiB), versioned JSON containing only the access token, an optional refresh token and the token type.

## 5. AAD

The AAD is `event-marketing:credential:{provider}:{connection_uuid}:{envelope_uuid}:{schema_version}:{key_id}`, built from the owning connection's provider and ID, which are passed by the caller from the connection row, plus the envelope row ID.

Row-swap analysis, now covered by a new DB-level test:

| Attack | Result |
|---|---|
| Instagram nonce and ciphertext copied into the LinkedIn envelope | Provider and connection in the AAD differ, so authentication fails |
| Whole Instagram envelope row moved under the LinkedIn connection, keeping its ID | Provider and connection differ, so it fails |
| Credential A copied into credential B | Connection and envelope IDs differ, so it fails |
| Schema version or key ID relabelled | Both are authenticated, so it fails |

In every case the connector fails closed (`*_CREDENTIAL_VAULT_UNAVAILABLE` or not connected), and unrelated destinations are unaffected. Residual risk: a same-row rollback is still possible (L3).

## 6. Nonces

Nonces come from `os.urandom(12)`, which uses the OS CSPRNG, on every `_encrypt`: store, refresh, reconnect and rotation. Nothing reuses or derives a nonce. A 96-bit random nonce under one key is safe far beyond this vault's write volume. The schema enforces `octet_length(nonce) = 12`.

## 7. Key storage

- **Production:** `FileCredentialKeyProvider` reads a JSON keyring from an absolute path that must be outside the repository. This is enforced in both config validation and the provider, with symlinks resolved.
- **Fails closed** on:
  - invalid JSON or duplicate keys
  - bad key IDs, bad Base64 or a wrong key length
  - a missing active key or an unknown key ID
  - group- or world-readable files (POSIX)
- **No auto-generation:** keys are never generated at startup. A missing keyring leaves the vault unavailable. Keys are not in PostgreSQL, Git or `.env.example`.
- **Key selection:** the active key is chosen deterministically by explicit `CREDENTIAL_VAULT_ACTIVE_KEY_ID`.
- **Backup guidance** requires separately protected keyring backups, not stored beside DB backups.
- **Path control:** keyring selection is deployment configuration only; there is no request-controlled path.

**Finding M1 (fixed):** the shipped `app/main.py` contained a constant vault key (`b"event-marketing-test-key-000000"`), used whenever `environment == "test"`. Test mode was accepted with any HTTPS origin. A deployment mistakenly run with `EMP_ENVIRONMENT=test` would therefore have encrypted real credentials under a key published in source, and also mounted test-only routes.

## 8. Rotation

Each envelope is rotated in its own transaction: decrypt with the recorded key, re-encrypt with the active key and a fresh nonce, update in place. The ciphertext and its key-ID metadata change atomically. A crash leaves either the old or the new committed envelope, never neither, and never a key ID pointing at the wrong key. Plaintext is never printed.

**Finding M2 (fixed):** rotation locked envelope → connection, while live credential use, refresh, reconnect and disconnect lock connection → envelope. Running rotation alongside live use could deadlock, and PostgreSQL would abort a participant. The new test proved the original code holds the envelope lock while waiting for the connection; it failed against the pre-fix script with `LockNotAvailable`.

**Finding L1 (fixed):** a mid-batch failure reported only an exception name. It now reports how many envelopes had already rotated, so the rerun is understood to resume safely.

## 9. OAuth state

- `secrets.token_urlsafe(32)`, which is 256 bits.
- Stored only as a SHA-256 digest, with a unique index. The raw state is never persisted.
- 10-minute TTL, bound to provider, application user and server session, with a fixed return destination.
- Consumed atomically under `SELECT … FOR UPDATE` before the code exchange. Strict format checks run before lookup.
- **Concurrent replay:** the new test fires 8 simultaneous consumptions of one state, and exactly one succeeds.

## 10. Callback and session

The callback requires a valid app session.

| Scenario | Outcome |
|---|---|
| Another browser or session | Rejected (session mismatch, tested) |
| After logout | 401 |
| Different user | Rejected |
| Provider mismatch | Rejected (state is provider-bound) |
| Stale or expired | Rejected |
| Duplicate | Rejected |
| Duplicate query parameters | Rejected |

**Account-linking CSRF:** an attacker cannot obtain an unconsumed state bound to the victim's session, so an attacker's authorization code cannot be attached to the victim's workspace. Login CSRF does not apply, because provider OAuth is never used as app login. Connect and Disconnect are POSTs protected by CSRF and same-origin checks.

**Open redirect:** none. Redirects go only to fixed `/settings?connection=…` values, and the provider comes from the route, not a parameter. No encoded, protocol-relative or user-supplied URL is ever used.

**Authorization codes** are length-bounded, used once for the exchange, and never stored, logged, audited or returned. Uvicorn access logs are disabled, and the docs warn about proxy query logs.

## 11. Reconnect

- **Ordering:** code exchange, scope check and identity validation all happen before the database transaction. Inside one transaction the vault encrypts and overwrites the envelope in place. Any failure (provider, scope, identity, account mismatch, vault) rolls back, and the existing working credential is untouched. This is tested.
- **Account swap:**
  - Instagram rejects a reconnected account that differs from the configured Professional account. Execution re-verifies the approved target against both configuration and the live `/me` account before every operation.
  - LinkedIn binds the member identity separately. Every operation re-checks that member's role on the approval's frozen Organization URN.
  - So an approval targeting Organization A or Instagram account A cannot silently execute against B.

## 12. Disconnect

Disconnect requires authentication and CSRF, and is limited to the owner. In one transaction under the connection lock it deletes the envelope and marks the connection DISCONNECTED, so the credential is unavailable immediately. Remote revocation then runs after the commit and is reported honestly (CONFIRMED, UNCONFIRMED or NOT_SUPPORTED). The audit records only the provider and the revocation category.

**No in-memory token cache exists.** Every connector operation decrypts afresh, and grants are short-lived frozen dataclasses with `repr=False` on tokens. A stale credential therefore cannot outlive a disconnect, refresh or reconnect.

## 13. Refresh and concurrency

- **LinkedIn** refreshes only with a stored refresh token, within 7 days of expiry, and never after refresh expiry. A rotated refresh token replaces the old one only after validated success.
- **Instagram** refreshes only a still-valid long-lived token at least 24 hours old.
- **Failures:** a provider rejection becomes REAUTH_REQUIRED, while a transient error keeps the still-valid token. A scope change forces re-authorization. There are no retry loops, and repeated 401s just keep REAUTH_REQUIRED.
- **Concurrency:** refresh serializes on `FOR UPDATE` of the connection, then the envelope. A waiter re-reads the committed newer envelope after acquiring the lock, so it cannot overwrite with a stale credential. This is tested with concurrent refreshes.
- **Finding L2:** the provider refresh call runs while those row locks are held (15-second timeout). This is the intended serialization, but it can briefly block reconnect, disconnect and rotation on the same provider.

## 14. Logging and redaction

- OAuth, vault and connector log lines carry only categories, provider names and counts. Vault errors are generic and use `from None`.
- `CredentialEnvelope.__repr__` omits ciphertext, and token dataclasses use `repr=False`.
- The unexpected-exception handler logs a fixed message only. httpx/httpcore loggers are forced to WARNING, and the CLI prints only counts and key IDs.
- **Finding L4:** Instagram's long-lived token exchange and refresh put `client_secret` and `access_token` in the query string, as the provider requires. httpx logs full URLs at INFO, which is suppressed by default (the app forces WARNING, and the smoke scripts don't configure logging). Operators must not raise the httpx loggers to INFO or DEBUG in production.

## 15. API and frontend exposure

The provider-related responses are:

- `/api/settings/oauth-connections`
- `/api/oauth/{provider}/connect` (authorization URL only)
- `/api/oauth/{provider}/disconnect`
- `/api/settings/linkedin-organization` and `/api/settings/instagram`

None returns a token, ciphertext, nonce, key ID, client secret, code, verifier or raw state. The connect response's provider URL necessarily contains the one-time state.

The status endpoint returns non-secret identity metadata (`account_id`: member URN or Instagram account ID), which the UI does not render (INFO).

**Finding L7:** the production branch of the LinkedIn and Instagram Settings endpoints (`environment != "test"`) is not exercised by automated tests. Inspection shows it returns only safe fields.

## 16. Test-only routes

The app's `/api/test` routers mount only when `environment == "test"`, and each handler re-checks the environment. The fake-provider routes live solely in `tests/browser_server.py`, which refuses to start outside test mode and isn't part of the installed package. `Settings.environment` defaults to `production`, which fails closed.

After the M1 fix, test mode additionally **requires a loopback origin** and uses a **random per-process vault key**. A new test confirms that a non-test app mounts no `/api/test` routes, has no scenario controls, and leaves the vault unconfigured without an external keyring.

## 17. Migration

`0009_credential_vault`:

- has no plaintext token columns and no secret defaults
- enforces provider and status checks, one connection per provider, one envelope per connection (unique), and a 12-byte nonce check
- indexes the unique state digest and stores no raw state or code
- uses a cascade foreign key from envelopes to connections, and from attempts to users and sessions
- has a clean downgrade, and copies no secrets anywhere

Upgrade from empty, `alembic check`, downgrade and re-upgrade all pass. The only change made was formatting (L5).

## 18. Backup and recovery

- **DB restored without the keyring:** envelopes are unreadable and connectors fail closed (tested with a wrong key).
- **Keyring with the wrong DB:** the AAD binds ciphertext to its provider, connection and envelope, preventing cross-row or cross-provider transplant.
- **Finding L3:** there is no anti-rollback protection. An attacker who can **write** to the DB can restore an older envelope row for the same connection (for example a pre-disconnect credential) and it will decrypt. Tokens stay unreadable without the keyring, and the attacker already controls app state. A durable fix needs a monotonic counter outside the DB (KMS or HSM), which is deferred with the KMS work.

## 19. Findings by severity

| ID | Severity | Finding | Status |
|---|---|---|---|
| — | CRITICAL | None found | — |
| — | HIGH | None found | — |
| M1 | MEDIUM | Constant vault key in shipped source, plus test mode allowed on non-loopback origins, made a misconfigured `EMP_ENVIRONMENT=test` deployment encrypt real credentials under a public key and mount test routes | **Fixed** |
| M2 | MEDIUM | Rotation lock order (envelope → connection) inverted relative to all live credential paths, risking deadlock during rotation | **Fixed** |
| L1 | LOW | Rotation failure gave no partial-progress information | **Fixed** |
| L2 | LOW | Provider refresh network call made while holding connection and envelope row locks | Accepted design; documented |
| L3 | LOW | No anti-rollback: a DB-write attacker can restore an older envelope for the same connection | Deferred (needs KMS/HSM counter) |
| L4 | LOW | Provider-mandated query-string secrets would appear in httpx INFO logs if an operator raised those loggers | Defaults safe; operational guidance |
| L5 | LOW | Baseline lint/format failed on the 0009 migration despite the report | **Fixed** (formatting only) |
| L6 | LOW | Consumed or expired OAuth attempts (digests only) are never purged | Deferred housekeeping |
| L7 | LOW | Production branch of the LinkedIn/Instagram Settings status routes is not exercised by tests | Inspected safe; coverage gap noted |
| I1 | INFO | Once one user owns a provider connection, another allowlisted user cannot reconnect it even after disconnect | Safe direction; operational note |
| I2 | INFO | OAuth status API returns non-secret account identifiers not shown in the UI | Acceptable |
| I3 | INFO | Windows skips the POSIX keyring permission check; docs require ACLs | Documented |
| I4 | INFO | The file keyring is cached for the process lifetime; key changes need a restart | Expected |
| I5 | INFO | No PKCE; confidential server-side flows with session-bound one-time state | Acceptable as documented |

## 20. Fixes made

- **`app/config.py`:** `environment=test` is rejected unless the app origin is loopback.
- **`app/main.py`:** the test-mode vault key is now `os.urandom(32)` per process; no key material remains in source.
- **`scripts/credential_vault.py`:**
  - Rotation locks connection → envelope, matching every live path.
  - It verifies the envelope still belongs to that connection.
  - It reports rotated and unchanged counts on failure (`RotationIncomplete`).
  - It accepts injected database and vault objects for tests.
- **`migrations/versions/0009_credential_vault.py`:** formatting only. No schema or revision change.

No security architecture assumption changed.

## 21. Tests added

`backend/tests/test_vault_security_review.py`, 6 tests:

- concurrent same-state callback consumption succeeds exactly once
- DB-level ciphertext and row transplant between providers and connections fails closed
- rotation lock order (proven to fail on the original code)
- rotation partial failure is honest and leaves the envelope untouched
- test mode is loopback-only, with a random per-process key
- a non-test environment mounts no test routes or controls and the vault fails closed without a keyring

## 22. Full test results

| Check | Result |
|---|---|
| Backend (PostgreSQL) | **279 passed, 0 failed** (273 baseline + 6 new) |
| Frontend unit tests | **55 passed** |
| Playwright, desktop + iPhone | **58 passed** |
| Migration checks | passed |
| Ruff check and format (after the L5 fix), Prettier, ESLint, type generation, build | passed |
| `npm audit` | 0 vulnerabilities |
| pip-audit | no known vulnerabilities |
| `git diff --check` | passed |
| Secret scan | no real secret material; only synthetic test constants |
| Source integrity | PASS |

## 23. Remaining limitations

- No anti-rollback protection (L3); a KMS/HSM key provider is deferred.
- Refresh holds row locks across a bounded network call (L2).
- Operators must keep httpx loggers at WARNING and keep proxy and access logs from retaining OAuth callback query strings (L4).
- The keyring file is a single-host secret. KMS integration and automated purge of stale OAuth attempts are deferred.
- Real provider behaviour (token shapes, refresh rotation) is verified only against fakes until a deliberate read-only smoke test is run with real credentials.

## 24. Recommendation

**APPROVED FOR REAL CREDENTIAL SETUP**, subject to these deployment conditions:

- Store the keyring on a secret mount outside the repository, with restrictive permissions or ACLs, backed up separately from database backups.
- Run with `EMP_ENVIRONMENT=production` (the default).
- Keep httpx log levels at WARNING.
- Configure the reverse proxy not to log OAuth callback query strings.
