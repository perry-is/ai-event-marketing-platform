# Source material, provenance and upload boundaries

WP0B proposal with WP0C clarification correction. **CONFIRMED project constraint:** the operator's live Google Drive documents are out of bounds during development. Codex/application development uses only files the developer manually exports/downloads or explicitly supplies. Do not access, modify, rename, move, overwrite or delete live Drive sources. No Drive SDK, connector, sync, fetch or credential scope is included. Future production integrations require a separate deliberate decision. App login with Google identity does not request Drive access.

## Intake and provenance

Typed text, pasted email/conversation, transcript/notes, rough concept, copied event details, a flyer/image and PDF are supported intake concepts. Text entry is one input area, not a mandatory questionnaire. A URL is recorded text/reference only in V1; no server fetch. Runtime need not import historical planning workbooks to create an Event. Operator-only Group Master import is a separate explicit operation, using the supplied export; its source rows remain intact.

Store originals once as private bytes or exact text with SHA-256, uploader/time, declared origin, kind, event/catalog owner and original display name. No client filesystem path becomes storage path. Each new upload/edit of pasted material produces a new record; duplicate hash can be identified but does not collapse ownership/provenance automatically. Source is evidence, not mutable runtime truth or a re-read live master.

Extraction returns proposals `{field_path, candidate_value, source_id, locator, short_excerpt, evidence_status, uncertainty_reason}`. Locators are paragraph/line, image region/page or workbook tab/cell. Do not require a graph or sentence-level citations for every generated adjective. Keep enough support for factual claims and conflict resolution. Model confidence is advisory and separate from CONFIRMED business acceptance.

Backend validates proposal shape, source existence, scalar bounds, dates/URLs and plausible support. New proposals never overwrite accepted facts. Show a concise fact summary; authenticated “Use these details” accepts supported proposals, advances Event revision and records provenance/audit. Low-confidence/conflicting values stay visibly unconfirmed: The operator can confirm the concise summary or correct just the uncertain item, without approving facts one by one. Known canonical/destination requirements return deterministic missing/invalid paths first; optional AI prioritizes/phrases the natural-language question. AI can flag ambiguity, but cannot make an arbitrary field mandatory. Blocking ambiguities are resolved before approval; irrelevant gaps do not create a questionnaire. Later conflicting source material produces a visible proposal, not automatic replacement. A rough concept can get a preliminary preview with assumptions marked; assumptions affecting public logistics/claims require confirmation or omission before approval. Unknown blank is not false, free, online or a recurring default.

The Event stores current field evidence and pending proposals. Snapshot stores facts actually used plus provenance references; audit stores accepted field-change patches/revisions without private source bodies. The original remains separately accessible to the authorized user. Revision requests are concise audit metadata; V1 does not need a full chat platform/history product.

## V1 security policy (proposed concrete starting limits)

| Boundary | Proposed deterministic behavior |
| --- | --- |
| General inputs | Plain UTF-8 TXT/MD, JPEG, PNG, WebP, PDF. Treat Markdown as text, never executable HTML. Pasted text max 2 MiB. No audio/video/archives/Office documents/macros in general upload until scoped parser support is added; transcripts can be pasted. |
| Operator Group import | XLSX only, separately gated; reject macro files, external relationships and unsafe/oversized ZIP members. Read data without executing formulas or fetching relationships. DOCX handoff stays development reference, not runtime parser scope. |
| Size/complexity | Image 10 MiB and decoded maximum 25 megapixels; PDF 20 MiB / 50 pages; XLSX catalog 10 MiB with bounded archive expansion/cell count. Total accepted source size per Event 100 MiB. These are engineering limits, ASSUMED; adjust only through explicit config/review. |
| Type validation | Allowlist extension plus detected MIME/signature and successful bounded parser; do not trust browser Content-Type. Reject mismatch, executable/script, SVG/HTML, encrypted/unparseable PDF, malformed image, path tricks and polyglot-like input. |
| Naming/storage | Backend-generated opaque UUID storage key, no user-controlled path segments; original filename sanitized for display/download only. Private storage outside app static/web root, no public bucket listing. Store hash and byte length; never overwrite an existing object. |
| Temporary processing | Unique restricted temporary directory; no outbound network or shell evaluation; resource/time limits for parsers, page/pixel/decompression caps. Delete temp/failed derivatives promptly. Original accepted bytes remain unchanged; previews/text extractions are separate objects. |
| Image/PDF display | Serve sanitized raster previews through authenticated reads. Original PDF downloads use safe attachment headers, correct MIME and `nosniff`; no embedded script/external-link execution on server. Decode/reencode preview images to remove active/irrelevant metadata, without changing originals. |
| Authorization | Every upload, extraction, preview/download and deletion checks enabled app user and expected owning Event/catalog. CSRF/origin safeguards on writes; quotas before buffering full input. No server secrets in previews, logs, client bundles or model prompts. |
| URL intake | Record HTTPS reference only; do not dereference in V1. Future fetch needs explicit SSRF defenses (DNS/redirect revalidation, private/metadata address deny, size/time bounds, no source-supplied credentials) and a separate scope. No live Drive URL fetching. |

Use maintained parsers and ordinary OS/process isolation; no paid antivirus service or upload pipeline platform is introduced. Bounded parsers do not guarantee perfect safety; fuzz/negative cases in Phase 1 validate the actual chosen libraries before enabling a file category. Reject/retain only metadata for rejected files; do not save executable bytes indefinitely. Extraction from safely accepted image/PDF may be simulated in Phase 1; acceptance limits/security are real.

## Untrusted content and privacy

Source text can quote “publish now,” contain instructions to models, or demand credentials. Those strings are data. Extraction has no publishing tool, protected-state mutation, credential access, URL-fetch tool or connector authorization. Backend validates output and limits it to proposal fields; human acceptance and deterministic approval/dispatch are separate operations. Model/system prompts must explicitly delimit source content, but prompts alone are not the permission barrier.

Do not forward whole source archives/contacts to AI unnecessarily. Select relevant excerpts/images only under deliberate provider activation and retention review. Detected raw credentials/API tokens quarantine the input: exclude it from AI, ordinary preview/download, proposals/public assets and logs, and show a safe reason. Operator can dispose of sensitive input through a deliberate private retention procedure; do not expose operational credentials through a source download. Application secrets are never injected into source content. Meeting logistics that require public disclosure must be explicitly reviewed; no credential-like joining secret is silently added to campaign copy.

## Retention and asset provenance

Default proposal: retain accepted originals and approved version assets for campaign history until operator-authorized deletion; no unconfirmed automatic expiry. Delete temporary parse derivatives after processing, and unreferenced failed-generation artifacts within seven days. Client retention expectations remain UNKNOWN; confirm before live rollout. Referenced assets cannot be removed by ordinary revision. Backups must preserve snapshot-to-asset integrity and be restore-tested.

Exceptional privacy removal tombstones source metadata and records the actor/reason; approved public snapshot may still contain derived business facts, so handle their removal as an explicit operator/legal workflow, not routine mutation of approval history. No silent purge of approved asset bytes. Retention beyond V1 is not promised.

Creative references are REFERENCE_ONLY until the operator approves use. Exact approved logos/partner marks are immutable supplied assets, not generated approximations. Creative composition uses separate derivatives; snapshots pin exact bytes. Source copyright/brand authority is not inferred from a flattened JPEG (see [brand audit](brand-asset-inventory.md)).

Security guidance is informed by the primary [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html): use type/size checks, server-generated names and storage outside the public web root. The limits and workflow above are this project's proposed policy.
