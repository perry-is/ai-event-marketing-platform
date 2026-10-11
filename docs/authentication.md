# Application access recommendation

WP0B proposal: one organization, primary user, one privilege level, no public registration or multi-tenant IAM. The administrative app and every business/file operation require server-side authorization. App login is separate from any future publishing-account OAuth.

## Realistic options

| Option | Fit / maintenance |
| --- | --- |
| Allowlisted existing Google account with OIDC and local server session | Recommended, ASSUMED the operator has a suitable account. Avoids app password/recovery infrastructure and uses existing provider recovery/MFA. Operator provisions exact permitted identity; no Drive/publishing scopes. |
| One local password account | Viable fallback if account assumption fails, but app must implement secure hashing, throttling, reset/recovery and MFA decision. More maintenance for one user; do not build it alongside OIDC by default. |

Recommend Google OIDC authorization-code flow through Authlib, followed by an opaque PostgreSQL-backed session. OIDC support exists in the primary [Google identity documentation](https://developers.google.com/identity/openid-connect/openid-connect) and [Authlib Starlette documentation](https://docs.authlib.org/en/v1.5.2/client/starlette.html). Provider consent/client setup is an operator prerequisite, not a the operator architecture question. Supported Authlib release/dependency versions must be pinned/rechecked at Phase 1 setup; no provider integration implemented here.

## Login and authorization

Backend creates one-use, short-lived state/nonce tied to browser; authorization-code flow uses PKCE where supported. Validate callback state, ID-token signature/JWKS, issuer, audience, expiry and nonce; reject replay. Request only openid/email/profile. Bootstrap expected verified account via operator-controlled allowlist, bind verified issuer/subject, then use that stable pair rather than mutable email for authorization. No arbitrary Google account gains access just by logging in.

After login rotate a cryptographically random session id; browser gets only the opaque cookie. Store token digest/session metadata server-side; discard identity provider access/refresh tokens because app login needs no Drive/platform resource access. Server-side OAuth client secret and session signing/encryption material stay in secret environment/store, never Git/browser/model prompts. No application API key is exposed in frontend config.

All reads/writes/downloads require enabled user and valid session, except login callbacks and minimal health/static assets. Existence of a UUID is not authorization. One privilege level is sufficient; no role matrix. The developer's access requires explicit provisioning if desired, not implicit developer admin access. Operator can disable user/revoke sessions without relying on provider logout.

## Sessions and recovery

Use Secure, HttpOnly, SameSite=Lax, host-only cookie on HTTPS; short-lived login-state cookie similarly protected. Proposed app session: 12-hour absolute expiry, one-hour idle expiry; rotate after authentication and revoke on logout. Approval requires authentication no older than 30 minutes, with fresh login if needed. These are ASSUMED security/UX defaults for Architect review, not client workflow evidence. Keep unsaved draft on reauthentication.

Require CSRF token plus same-origin checks for cookie-authenticated mutations, including approval/execute/manual completion; GET has no side effects. Production frontend/API share one origin. Reject unexpected CORS origins; no wildcard credential CORS. Rate-limit login/callback/upload/expensive operations with bounded single-instance policy; redact tokens/cookies from logs. Use provider MFA and account recovery; do not create emailed plaintext recovery links/passwords. If primary identity is lost, operator verifies the owner out of band, rebinds identity with audit and revokes all old sessions.

If local-password fallback is later selected, use a maintained Argon2id implementation, server-only hashes, no plaintext logging, controlled one-time recovery and throttling; scope/review that change before coding. No dual auth system is proposed.

Development/test identity stub is allowed only in explicit loopback/test mode. Production startup refuses test identity, disabled authorization or publish-enabled simulation configuration. Phase 1 deployed demo requires actual allowlisted login; fake publisher adapters never justify public access. Local tests mock the identity provider while exercising backend session/authorization guards.

Session policy follows the primary [OWASP Session Management guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html); expiry values above are project proposals. PWA cache policy in [application boundaries](application-boundaries.md) prevents private content remaining in offline caches after logout.
