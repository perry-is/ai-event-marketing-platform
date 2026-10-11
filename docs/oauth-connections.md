# LinkedIn and Instagram OAuth connections

WP1M adds member-authorized OAuth connections for the existing LinkedIn Organization and Instagram Professional LIVE adapters. The Event Marketing Google OIDC login remains a separate identity boundary. Provider OAuth grants are never used as application login sessions.

## Connection flow and state security

An authenticated user starts a provider connection from Settings. The server creates a 256-bit random state value, stores only its SHA-256 digest with the provider, application user, current server session, fixed `SETTINGS` return destination, creation time, and ten-minute expiry, and returns a provider authorization URL built from server configuration. The browser does not choose scopes, redirect URI, provider endpoint, or return destination.

The provider callback requires the same valid application session. The server compares the state digest, provider, user, session, expiry, and unused status under a row lock, then consumes the attempt before exchanging the code. A state can succeed once. Mismatches, expired state, provider denial, duplicate query parameters, and exchange errors return a fixed Settings result; provider error descriptions and callback codes are not reflected. There is no caller-controlled redirect. The callback uses the existing `SameSite=Lax` application cookie, and `require_session` enforces same-origin plus CSRF token checks for Connect/Disconnect POST requests.

OAuth client secrets remain server-side deployment settings. The application disables Uvicorn raw access logging because callback query strings contain short-lived authorization codes. Production reverse proxies and hosting logs must also avoid retaining OAuth callback query strings longer than operationally necessary.

## Provider contracts

### LinkedIn Organization

The app uses LinkedIn's confidential web-server Authorization Code flow. Requested scopes are `openid`, `profile`, `r_organization_social`, `r_organization_admin`, and `w_organization_social`, matching the existing connector's member identity, Organization-role, and post-read/write requirements. The current OAuth token response scope value is URL-decoded and checked; a missing scope field follows OAuth's rule that the granted scope is identical to the requested scope when omitted. LinkedIn OIDC UserInfo establishes the connected member subject. The Organization target is a separate identity: existing LIVE readiness checks the configured Organization and member's posting role before approval/execution.

The application stores an access token and only stores a refresh token if LinkedIn returns one. Programmatic refresh is not available to every LinkedIn app; when no supported refresh token exists, a grant that expires requires explicit reconnect. When a refresh token exists, refresh is serialized with a PostgreSQL row lock, attempted before access expiry, and rotated refresh material replaces the old value only after validated success. A provider 401 marks the saved connection `REAUTH_REQUIRED`; it is not retried indefinitely or silently switched to simulation.

### Instagram Login

The app uses the Instagram Login authorization-code flow, not the Facebook-Page-linked login path. It requests only `instagram_business_basic` and `instagram_business_content_publish`. The server exchanges the short-lived token for a long-lived token, then identifies the account through Instagram Login's `/me` endpoint and validates the configured Professional account ID and Business/Creator account type. A successful token response can omit an explicit scope list; in that OAuth case the stored scope metadata represents the exact requested permissions granted by the successful consent flow.

The provider refresh operation is used only for a still-valid long-lived token that is old enough for refresh. Expired, revoked, or otherwise unusable grants require reconnect. A Graph API invalid-token response (including code 190) marks the saved connection `REAUTH_REQUIRED`. Instagram has no remote revocation operation implemented here; disconnect removes the local encrypted token and reports that remote revocation was not supported through this connection.

## Persistence and UI

One `provider_connections` row per supported provider stores non-secret identity and lifecycle metadata. One `credential_envelopes` row stores the versioned encrypted payload. Reconnect does not replace the prior envelope until token exchange, scope and identity validation, key availability, and encryption all succeed in one database transaction. Failed reconnect leaves the existing working credential unchanged. Disconnect requires authentication and CSRF, deletes the encrypted envelope locally, marks the connection disconnected, and then attempts LinkedIn revocation where supported. The UI distinguishes `Connected`, `Needs authorization again`, and not connected, and shows safe account name, scope status, and expiry only. It never returns or renders access/refresh tokens, client secrets, key IDs, ciphertext, authorization codes, or raw provider responses.

Connector registration and static capabilities remain fixed for the process lifetime, while LinkedIn and Instagram account/credential readiness is resolved dynamically through their credential-provider boundary on each readiness evaluation. Connect and Disconnect therefore affect Settings and Approval preview immediately without registry reconstruction or restart. Both surfaces use the same underlying provider-connection/vault state; full publishing readiness can still differ from the simpler connected status. OAuth status metadata does not itself make a destination production-ready: LinkedIn Community Management approval and Page role checks still apply, as do Instagram Professional-account, app-access, scope, quota, and public-media-delivery checks.

## No live account test in WP1M

Automated backend and Playwright flows inject fake OAuth gateways and deterministic test keys. They do not call LinkedIn or Meta. The existing smoke commands use read-only credential access and make no provider request when no stored connection exists. WP1M did not configure real app credentials, authorize a real account, or perform a provider write. Once deliberate credentials are supplied, smoke checks may perform only provider reads; publishing remains separately controlled by the existing LIVE destination plan and execution approval.

## PKCE decision and references

WP1M implements the confidential server-side web flows documented by both providers and uses high-entropy one-time state bound to the existing authenticated server session. It does not send a PKCE verifier: the reviewed provider flows used here document client-secret-backed server token exchange rather than requiring PKCE for this web-client registration. A future public/native client or provider requirement would require a separate contract change; provider PKCE semantics are not inferred from the Google application login.

Official references checked 2026-10-07:

- [LinkedIn 3-legged OAuth authorization-code flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
- [LinkedIn programmatic refresh tokens](https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens)
- [Instagram Login getting started](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/get-started)
- [Instagram Login content publishing and permissions](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing)
- [Instagram Login access-token refresh](https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token)
