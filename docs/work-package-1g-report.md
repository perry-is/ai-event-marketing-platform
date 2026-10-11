# Work Package 1G report

**Status: COMPLETE.** WP1G adds one version-bound campaign graphic flow. It does not add group
recommendations, live destination APIs, OAuth, or live publishing. The clean baseline was
`f209b45a7f61f618abcf225e9b18bb7e4bd4f1ec`; source integrity passed before and after implementation.

1. **Implementation summary.** A replaceable image-provider boundary supplies one visual layer;
   a deterministic renderer composes exact Event text; the final private asset becomes part of an
   immutable successor CampaignVersion.
2. **GeneratedAsset persistence.** Migration `0005_campaign_graphics` adds immutable `assets`
   records for provider visual layers and final composed graphics, including type, generation mode,
   model/prompt metadata, source asset, storage key, MIME, dimensions, byte size, SHA-256, provenance,
   and creator/time. Database triggers prevent asset mutation.
3. **CampaignVersion asset association.** An append-only `campaign_version_assets` manifest records
   at most one `PRIMARY_GRAPHIC` per version. Database checks ensure the asset belongs to the same
   campaign and agrees with the version snapshot.
4. **Version immutability.** Adding a first graphic or revising a graphic creates a successor
   CampaignVersion. Existing versions, assets, and manifests cannot be replaced or edited.
5. **Snapshot hash.** Text-only hash projection stays byte-semantically compatible. Graphic snapshots
   include asset ID, role, final content hash, composition version, and layout variant; private path,
   timestamp, and provider request ID do not affect the hash.
6. **Approval.** The existing exact-version approval remains attached to its snapshot/hash. A new
   graphic version does not inherit approval. Historical approval remains associated with its
   original graphic and is superseded under the existing material-successor rules.
7. **Execution.** Simulated automatic execution records the graphic ID/hash from the approved
   version. Facebook Groups' manual package includes the same graphic reference and authenticated
   preview path. There are no live destination calls.
8. **OpenAI image adapter.** The production adapter uses the OpenAI Images API generate/edit
   boundary, one request per operation, with validated returned bytes, a 45-second timeout, and
   SDK retries disabled. No provider conversation or implicit retry loop is used.
9. **Image model configuration.** `OPENAI_IMAGE_MODEL` is configurable and defaults to
   `gpt-image-2.5-flare`. Production configuration requires image mode `openai` and the existing
   server-only OpenAI API key. Tests explicitly inject the fake provider.
10. **Generation behavior.** A text-only current version can generate one graphic. The visual layer
    and final graphic are stored privately; a new version is created only after provider output,
    composition, and persistence succeed.
11. **Editing/revision.** Visual requests create a new generated layer and successor version.
    `Make the title larger` and `move the date higher` use the small deterministic layout variants
    without an image call, while still creating a new composed asset/version. Requests to change
    Event facts are rejected for correction in Event detail.
12. **Prompt/version.** Prompt contract `campaign-visual-v1` sends bounded Event topic/family/delivery
    context and creative/revision direction as untrusted visual preferences. It explicitly prohibits
    text, numbers, logos, watermarks, and typography and requests calm overlay space.
13. **Composition renderer.** Pillow's deterministic `campaign-square-1080-v1` renderer makes a
    square 1080×1080 PNG with fixed panel/margins and controlled wrapping using Pillow's bundled
    Aileron font. It renders exact Event title, date/time with IANA timezone, and location/virtual
    designation. No logo asset was available with confirmed approval, so none is included.
14. **Event text/data sent.** The adapter does not select source material or pass title, schedule,
    location, and price as canonical Event-context fields; it sends topic/family/delivery plus the
    bounded campaign creative-direction and revision strings. Those preference strings could
    themselves mention Event details. The renderer reads exact title/date/time/timezone/place from
    the CampaignVersion's Event snapshot and draws them locally without rewriting.
15. **Brand/logo handling.** The source audit confirms embedded marks and a candidate composite
    banner, but not an approved standalone logo. No supplied source original was modified, extracted,
    or treated as an approved runtime logo.
16. **Format/dimensions.** One square social/event format is generated at 1024×1024 by the provider
    and composed at 1080×1080. Provider outputs are bounded at 12 MiB, 4096 pixels per side, and 25
    megapixels; final output is PNG.
17. **Storage/preview.** Opaque private storage keys use the existing storage abstraction. Authenticated
    backend preview serves validated bytes. No public asset URL or filesystem path is exposed.
18. **Failure behavior.** Invalid output, provider errors, or composition/persistence failure leaves
    current version and prior approval unchanged and creates no successor; partial private writes
    are removed if the database transaction fails.
19. **Cost controls.** One generation or edit call, no variants, hidden retries, critique calls, or
    model escalation. Deterministic layout changes make no image-provider call.
20. **Backend tests.** Full PostgreSQL-backed suite: **151 passed, 0 failed**. This includes provider,
    asset/version immutability, approval, manual and automatic exact-version execution, failure
    atomicity, and text-only hash compatibility.
21. **Frontend tests.** **27 passed, 0 failed.** Includes empty graphic state, generation/loading,
    preview, graphic revision cues, history, approved/current distinction, and safe failure UX.
22. **Playwright.** **16 passed, 0 failed** (eight existing workflow tests each on desktop Chromium
    and iPhone 13 Chromium). The suite includes graphic generation/revision, exact approved asset
    in simulated/manual outcomes, historical version preview, Event-fact correction rejection,
    authenticated preview, and no horizontal overflow on mobile.
23. **Migration.** The dedicated PostgreSQL migration test passed (**1 passed**) from an empty
    database, repeated upgrade, downgrade, and re-upgrade. The full backend suite also passed.
24. **Live image smoke.** **DEFERRED** because `OPENAI_API_KEY` is not configured in the local
    application environment. Image mode is fake; no live provider call was made.
25. **Security review.** Server-side API key only; authenticated previews; opaque storage paths;
    image bytes validated before storage/read; provider errors are safely normalized; no source
    files, credentials, or authorization data are included in prompts or metadata.
26. **Files created/modified.** 27 intended files: `.env.example`, `README.md`,
    `docs/campaign-graphics.md`, this report; `backend/app/{campaign_routes.py,campaign_service.py,
config.py,execution_service.py,graphics.py,graphics_service.py,main.py,models.py,routes.py,
simulated_connectors.py}`, migration `0005_campaign_graphics.py`, backend test fixture updates and
    `test_graphics.py`/`test_graphics_provider.py`; `frontend/e2e/campaign-review.spec.ts` and
    `campaign-graphics.spec.ts`; `frontend/src/App.module.css`, generated `api.generated.ts`,
    `campaigns.tsx`, and `campaigns.test.tsx`.
27. **Dependencies.** No new package was added; existing OpenAI SDK, Pillow, SQLAlchemy, and
    PostgreSQL infrastructure are reused.
28. **Architecture conflicts.** None found in the reviewed versioning, approval, execution, or
    storage contracts. No existing approved architecture document required changes.
29. **Deferred scope.** Facebook Group recommendation, live destination APIs/OAuth, live publishing,
    additional platform ratios, approved standalone logo use, and a freeform poster editor remain
    deferred. Live Google OIDC remains governed by its prior credential-setup limitation.

**Final quality checks.** Frontend lint/type check, Prettier, and production build passed; Python
Ruff lint/format passed; npm audit found **0 vulnerabilities**; pip-audit found no known
vulnerabilities (it skipped only the local unpublished application package); `git diff --check`,
changed-file secret scan, and source-integrity validation passed. API types were regenerated and now
describe the authenticated PNG preview response. No Event Marketing source/reference original
changed. All checks were run against the final implementation before the checkpoint commit.
