# LinkedIn Organization connector

Updated for WP1K on 2026-10-07. The connector implements the LinkedIn Organization live path behind the approved per-destination LIVE/SIMULATED plan. Event Marketing has not been connected to a real LinkedIn app, member, or Page; no production readiness or access tier is claimed.

## Scope and access gate

This connector publishes ordinary organic content to one explicitly configured LinkedIn Organization. It does not post to personal profiles and does not implement comments, replies, reactions, targeting, sponsored content, LinkedIn Events, employee advocacy, advertising, or analytics.

Community Management is a vetted LinkedIn API product. Development access is intended for integration building/testing with restrictions; Standard access is the tier intended for production. LinkedIn requires an application access review and then a Standard tier request with a screencast. The Settings page reports the configured tier as operator-provided information; it is not proof that LinkedIn granted it. The current fake provider verifies application behavior only.

The app requires a member-authorized three-legged OAuth grant with the permissions `w_organization_social`, `r_organization_social`, and `r_organization_admin`; `openid` and `profile` are included for the member identity boundary. The member must have an approved Organization ACL role. The Posts API lists `ADMINISTRATOR`, `DIRECT_SPONSORED_CONTENT_POSTER`, and `CONTENT_ADMIN`; the Organization ACL role reference names the last role `CONTENT_ADMINISTRATOR`. Because those exact enums differ, this implementation accepts only the two role values spelled identically in both sources and blocks the unresolved Content Admin case rather than guessing an alias.

WP1M implements the member-authorized server-side OAuth connection and stores delegated tokens only in the AES-GCM credential vault. `LinkedInCredentialProvider` continues to isolate token retrieval from the Posts adapter. Real Event Marketing connection and LIVE readiness remain unverified until app credentials, OAuth consent, suitable Community Management access, and Organization role access are configured. Do not store access or refresh tokens in plaintext fields. See [OAuth connections](oauth-connections.md) and [credential vault](credential-vault.md).

## Server configuration and plan identity

`EMP_LINKEDIN_ORGANIZATION_EXECUTION_MODE` is server-owned and defaults to `SIMULATED`. A configured LIVE mode does not itself activate a connector or authorize a member. New destination plans freeze the numeric Organization ID and `urn:li:organization:{id}`. Display names are informational and do not define identity. Existing approvals retain their immutable mode and Organization; changed server configuration blocks an old plan instead of redirecting it.

`EMP_LINKEDIN_API_VERSION` defaults to `202609`, the latest supported Marketing API version confirmed on 2026-10-07. The app validates it against its maintained supported-version list and rejects `202510`, which sunsets on 2026-10-15. Every versioned REST call includes `Linkedin-Version` and `X-Restli-Protocol-Version: 2.0.0`. Version allowlist maintenance is an operational task when LinkedIn releases or sunsets versions.

The environment may contain `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI`, `LINKEDIN_ORGANIZATION_ID`, `LINKEDIN_ORGANIZATION_NAME`, and `LINKEDIN_COMMUNITY_ACCESS_TIER`. These are server-only settings. Supplying app values is not equivalent to an OAuth grant, role, Page connection, or LinkedIn approval. The Settings response never returns secrets, tokens, authorization codes, or raw provider payloads.

## REST behavior

The adapter uses the current REST Posts API at `https://api.linkedin.com/rest/posts`; it does not use deprecated `ugcPosts`. It mechanically renders the exact approved Organization post copy as `commentary`, uses the frozen Organization URN as `author`, and requests ordinary public main-feed distribution. It does not rewrite or shorten approved copy. Text-only versions remain text-only. If a version contains a graphic, the adapter uses the exact version-bound bytes and content hash.

For an image, it initializes an Images API upload with the frozen Organization as owner, uploads the exact PNG/JPEG bytes using LinkedIn's documented binary `PUT` flow, checks the returned image resource until it is `AVAILABLE`, then creates the post referencing that exact image URN and Event title as alt text. If processing is not verified, it does not create the post. An image belonging to a newer, unapproved version is never selected.

The first verified post is bound through the generic external resource binding to the local Event and exact Organization. The API response's `x-restli-id` supplies the Post URN; the adapter persists the binding before readback to prevent duplicates after a lost verification response. Success requires a readback of that exact URN confirming author, commentary, published lifecycle state, and image URN when applicable. LinkedIn does not supply a canonical public URL through the current read path used here, so the app does not invent one.

An approved copy-only successor may update the existing bound Post through the documented partial update of `commentary`, followed by readback. If a successor changes the graphic, or any other unsupported immutable content, execution blocks before a provider write. The app does not delete or repost to emulate replacement. An uncertain create without a known Post URN becomes `UNKNOWN`; no retry is offered. A known URN can be read for reconciliation. The connector never searches by post text and never auto-binds a similar post.

Provider 401/403/429/validation/network/server responses are normalized into application error categories. Raw response bodies and bearer tokens are not sent to the UI or logged. Rate limits are surfaced for deliberate retry; the adapter has no aggressive retry loop and never retries uncertain writes automatically.

## Readiness and verification

The protected Settings card separates connector implementation, configured LIVE/SIMULATED mode, Page identity, account-grant/role checks where available, and configured Community Management tier. “Connected” does not imply Standard-tier production approval; the displayed access tier is only the operator-configured claim until a real Developer Portal review is verified.

`backend/scripts/linkedin_smoke.py` is safe and read-only. With no encrypted LinkedIn connection it reports `DEFERRED` and sends no provider request. If a real connection is deliberately configured, the command decrypts the current non-expired token without refreshing or changing the database, then verifies the member, scopes, Organization ACL, and versioned read access. Automated tests and Playwright use only fake OAuth and REST transports and make zero LinkedIn network calls. No real LinkedIn post was created during WP1M.

## References checked 2026-10-07

- [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-09) — organization permissions/roles, Posts API, version/protocol headers, and post operations.
- [Marketing API versioning](https://learn.microsoft.com/en-us/linkedin/marketing/versioning?view=li-lms-2026-09) and [migration status](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/migrations?view=li-lms-2026-09) — `202609` is current; `202510` sunsets 2026-10-15.
- [Organization Access Control by Role](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/organizations/organization-access-control-by-role?view=li-lms-2026-09) — Organization ACL query and exact role names.
- [Images API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/images-api?view=li-lms-2026-09) and its linked [official image upload instructions](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/vector-asset-api?view=li-lms-2026-09) — initialize/upload/readiness flow and media ownership. The linked upload guide specifies raw-byte `PUT` with bearer authorization for the returned upload URL; the Marketing API version and Rest.li headers remain on the versioned Images API calls.
- [Community Management App Review](https://learn.microsoft.com/en-us/linkedin/marketing/community-management-app-review) — Development and Standard access review.
