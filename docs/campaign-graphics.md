# Campaign graphics

WP1G implementation contract. A campaign graphic is reviewed business content, so it belongs to
one exact immutable CampaignVersion. It is not an attachment that can be swapped under a version
after approval.

## Asset and version ownership

The `assets` table stores immutable generated artifacts. WP1G uses two asset types:

| Asset type         | Contents                                                             | Purpose                                                                        |
| ------------------ | -------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `VISUAL_LAYER`     | The validated image-provider output without authoritative event text | Retained privately so a later visual revision can edit the prior visual layer. |
| `CAMPAIGN_GRAPHIC` | The deterministic, composed PNG shown to the operator and used by execution  | Exact reviewed business-significant graphic.                                   |

`campaign_version_assets` is an append-only manifest with one `PRIMARY_GRAPHIC` row at most per
CampaignVersion. The version snapshot carries that asset's ID, role, SHA-256, composition version,
and layout variant. Database constraints/triggers reject a late attachment, reassignment, or
mutation. A database constraint checks that the inserted manifest and snapshot refer to the same
campaign-owned asset and content hash.

Generation from a text-only version creates a new visual layer and final graphic, then atomically
creates the successor CampaignVersion. A visual edit creates a new visual layer and final graphic,
then another successor. A supported deterministic layout adjustment creates a new composed graphic
from the existing visual layer and also creates a successor. The earlier CampaignVersion and all
its bytes/manifest links remain unchanged. Copy-only revisions preserve the current graphic unless
the graphic itself is revised; they reference the same immutable asset without duplicating bytes.
A failed provider call, validation, composition, or
successor transaction creates no CampaignVersion; newly written files are removed if persistence
fails.

## Snapshot hash and approval

The existing text-only hash projection is unchanged for historical compatibility. Graphic-bearing
versions add a `primary_graphic` projection containing the asset identity, role, final content hash,
composition version, and layout variant. Storage keys, timestamps, preview paths, and provider
request IDs are excluded. Two graphic versions with different bytes therefore have different
hashes; the hash does not depend on a machine-specific path or temporary URL.

Approval remains bound to the exact version ID/content hash and independent immutable destination
plan/hash. Connector runtime mode belongs to that destination plan, not the CampaignVersion hash.
Adding or revising a graphic creates a successor and goes through the existing
successor workflow. Any old approval remains historical and bound to its old graphic. It is not
copied to the successor. A successor requires fresh approval. Existing material-revision handling
withdraws active authority from the superseded version while preserving its history.

Execution builds its destination plan from the approved version's own manifest and verifies the
snapshot/hash. Simulated automatic outcomes record that version's graphic identity and hash.
Facebook Groups' manual package contains the same identity/hash and authenticated preview path. A
later graphic revision cannot change an older plan, execution result, or manual package.

## Provider boundary and prompt

`ImageProvider` exposes `generate(prompt)` and `edit(source_bytes, prompt)`. The production adapter
uses one [OpenAI Images API](https://developers.openai.com/api/docs/guides/image-generation)
generate or edit call per request and asks for one medium-quality 1024×1024 PNG. Its request
timeout is 45 seconds and SDK retries are disabled. It does not keep a provider conversation or
retry/regenerate behind the operator's back.
`OPENAI_IMAGE_MODEL` selects the model (default `gpt-image-2.5-flare`); model choice is not
hard-coded in domain code. Production requires `EMP_IMAGE_GENERATION_MODE=openai` and the existing
server-only `OPENAI_API_KEY`. Development and test default to the deterministic fake provider.

The prompt contract is `campaign-visual-v1`. It supplies only topic, event-family label, and
delivery from canonical Event context, plus campaign creative direction and the operator's optional visual
revision request. Those values are treated as untrusted visual preferences. It asks for one
professional event visual, useful space for a deterministic text panel, and no words, numbers,
logos, watermark, or typography. The visual palette may draw on navy/near-black, electric blue,
orange, and white, with connectivity motifs where suitable; it does not force every motif into each
graphic.

The adapter does not select uploaded source material/source text or pass the canonical title,
schedule, location, price, account IDs, or secrets as Event-context fields. The creative-direction
text and revision instruction are sent as bounded preference strings and could themselves mention
some Event details; they are not stripped for fact content. For an edit, the previous generated
visual-layer bytes and bounded prompt are sent; the composed image with authoritative text is not
used as the edit source. Provider request ID and model ID may be stored as operational provenance,
but API keys, authorization headers, base64 response payloads, and hidden reasoning are not
persisted or logged.

## Deterministic composition and brand assets

The local Pillow renderer owns the visible canonical Event text. Its fixed `campaign-square-1080-v1`
composition places the exact Event title, localized date/time with the Event's IANA timezone, and
location/virtual designation over an opaque high-contrast panel with safe margins. The only current
layout variants are `STANDARD`, `TITLE_LARGER`, and `DETAILS_HIGHER`. A request to change the Event
date/title/location is redirected to Event correction. Unsupported text/font/layout requests are
rejected with a useful response rather than passed off as image-model control. Text wrapping and
panel positions are deterministic; output is a 1080×1080 PNG. The renderer does not rewrite Event
values.

The renderer uses Pillow's bundled Aileron font, not a font installed only on the developer's workstation.
The repository audit found no approved standalone Event Marketing logo. Supplied JPEGs contain
embedded marks and one candidate composite brand banner, but approval/extractable logo authority is
unknown. WP1G therefore adds no logo and does not ask an image model to recreate one. Exact approved
brand assets can be introduced later as a derived-use input while preserving their originals.

## Validation, storage, and preview

Provider output and every source visual are decoded locally and checked for allowed image format,
single-frame content, minimum/maximum dimensions, a 25-megapixel bound, and a 12 MiB byte bound.
The final renderer emits PNG and the service calculates the final SHA-256, byte count, and dimensions
from the actual bytes. Asset keys are opaque server-generated values in the existing private
storage abstraction; original names and arbitrary client paths are never used. Both the source
visual layer and final composed graphic are retained privately because future edits need the former
and exact review/execution needs the latter.

`GET /api/campaign-versions/{version_id}/graphic` is authenticated and returns inline image bytes
with a restrictive content type. The UI and manual package use that backend path; there is no
public/permanent URL and no filesystem path is exposed. The same asset integrity metadata is checked
when the image is read for editing/preview.

## Failure and cost controls

One image-provider call is made for generation or visual edit; deterministic layout-only requests
make no provider call. There are no candidate grids, critique calls, hidden regeneration loops,
automatic model escalation, or provider retries. Invalid/oversized output fails closed. Provider or
composition failure leaves current version, prior approval, and history intact, so the operator may retry.

The fake provider implements the same interface and produces a deterministic synthetic image. It
is used for development, tests, and browser acceptance without network calls. Live image smoke
testing is optional for this package and is reported separately when credentials are unavailable.

## Deliberately outside this package

No destination APIs/OAuth, Facebook Group ranking, multi-format asset suite, editable poster
canvas, logo reconstruction, or live publishing is included. One primary square graphic is
associated with each version. A material visual or layout change always means a successor version
and fresh approval.
