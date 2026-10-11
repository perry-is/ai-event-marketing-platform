# Instagram Professional Account connector

WP1L adds a LIVE adapter for single-image feed posts using Instagram API with Instagram Login. The destination remains SIMULATED by default; the server owns `INSTAGRAM_EXECUTION_MODE`, and every new Approval freezes the selected account ID and runtime mode in its destination-plan entry. Existing Approvals are never reinterpreted from current configuration.

## Provider contract

The implementation uses `https://graph.instagram.com` and Meta Graph API `v26.0`, which Meta identified as the latest supported version on 2026-10-07. The version is configured by `EMP_INSTAGRAM_GRAPH_API_VERSION`; malformed values fail configuration. The request boundary sends a bearer token and supports only the operations needed here: `/me`, `/{account-id}/content_publishing_limit`, `/{account-id}/media`, `/{container-id}`, `/{account-id}/media_publish`, and `/{media-id}`. It requests only `instagram_business_basic` and `instagram_business_content_publish`.

Instagram Login's `/me` response is checked for the exact numeric Professional account ID, username, and account type `Business` or `Media_Creator`. The publishing-limit endpoint is consulted before Approval readiness and before a write; quota is not hardcoded. The API returns provider usage/total values. A profile name or username alone is never accepted as account identity.

Official references checked on 2026-10-07:

- [Instagram Login getting started](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/get-started)
- [Instagram Login content publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing)
- [Instagram User reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user)
- [Instagram publishing limit reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit)
- [Instagram media reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-media)

API capability does not establish Event Marketing's app review/access, account connection, granted scopes, or production readiness.

## Credential boundary and readiness

`InstagramCredentialProvider` supplies a short-lived access grant to the adapter. WP1M adds the Instagram Login callback and encrypted credential vault; connection readiness is still unverified until real app credentials, consent, app access, and account setup are configured. Tokens are never stored in plaintext or returned to the browser. The read-only smoke command reports `DEFERRED` and sends no provider request when there is no stored connection. If configured, it reads the current token from the vault without refreshing or changing the database. See [OAuth connections](oauth-connections.md) and [credential vault](credential-vault.md).

Settings separates connector implementation/mode, account eligibility, required scopes, and media-delivery configuration. A LIVE readiness check also reads `/me` and the publishing quota. The public HTTPS host is a deployment configuration assertion; the application does not claim it has independently tested internet reachability. Meta app access/review remains an operator verification item.

## Approved content and one-post lifecycle

The adapter renders only the exact approved CampaignVersion: its exact Instagram caption and its `PRIMARY_GRAPHIC`. Instagram V1 requires both. Missing graphic/caption, mismatched account, absent provider grant, quota exhaustion, invalid media, or unsafe server setup blocks before mutation. No unapproved current version or newer graphic can be substituted. The campaign content hash and destination-plan hash remain separate; no runtime mode or credential enters the campaign hash.

The approved graphic is private in application storage. Before `/{account-id}/media`, the server signs a token binding the asset ID, stored hash, and exact CampaignVersion. The temporary public endpoint permits only GET/HEAD and serves only those verified bytes; it has no session requirement because Meta fetches the media URL. The high-entropy HMAC token expires after 30 minutes, cannot name an arbitrary file, and yields no permanent URL. Uvicorn raw access logging is disabled in the application; deployments must also avoid retaining signed URL paths in proxy/CDN logs beyond normal operational needs.

Meta's publishing contract requires JPEG. The server verifies the approved source hash, MIME/decoded-format match, static single-frame image, 320–1440 pixel edges, supported 4:5–1.91:1 aspect ratio, and bounded size. It performs only a deterministic RGB JPEG transcode at quality 100 with no crop or resize. Transparent images are blocked rather than flattened against an invented background. The resulting JPEG remains a temporary serving representation; it does not replace or mutate the approved source asset.

The write sequence is:

1. Revalidate Approval/version/plan, frozen account, credential grant, and existing resource binding.
2. Confirm no Instagram post is already bound to this Event and check the provider quota.
3. Sign a version/asset-specific URL and create one image container with the exact approved caption.
4. Poll the exact container at most five times, 60 seconds apart. Only `FINISHED` proceeds; `ERROR`/`EXPIRED` fail, and unresolved processing remains uncertain.
5. Call `media_publish` once, bind the returned media ID immediately, then GET that exact media ID.
6. Mark success only when readback confirms exact ID, caption, `IMAGE` type, account username, and a canonical HTTPS Instagram permalink.

The permanent resource binding is keyed by destination, frozen account identity, and local Event. It stores the verified media ID/permalink and approved asset/caption identity. A known ID is reconciled by exact readback. A known container is inspected by exact ID; a `PUBLISHED` container without a media ID remains uncertain. An unknown create/publish result is never repeated blindly or fuzzy-matched by caption. Retry is limited to the Instagram execution and is allowed only after deterministic confirmation that no post was published.

V1 treats a published Instagram post as immutable. If a later CampaignVersion is approved for the same Event, Instagram blocks before any provider mutation, whether its caption or graphic changed. There is no automated edit, delete, or repost path, and no second post is created. A future replacement workflow needs explicit product approval.

## Scope and operational limits

Only one-image feed posts are included. Reels, video, carousel, Stories, tags, comments, messaging, insights, ads, scheduling, Facebook Pages, and Facebook Groups remain out of scope. Tests use a stateful fake over the real LIVE adapter and make zero Meta calls. A read-only real smoke is deferred until secure credential connection exists; this package never publishes a real post.

Meetup LIVE currently blocks versions with a campaign graphic because its binary photo upload contract is unverified, while Instagram LIVE requires an approved graphic. Those two LIVE destinations therefore cannot both execute for the same graphic-bearing CampaignVersion in the current reviewed design. Tests keep Meetup SIMULATED when exercising an Instagram LIVE graphic.
