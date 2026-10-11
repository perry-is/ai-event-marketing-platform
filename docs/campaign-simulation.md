# WP1D campaign simulation

WP1D adds the first authenticated campaign review path after canonical Event readiness. It uses a deterministic local template service to prove Campaign and immutable CampaignVersion behavior. The generated wording is a product-flow fixture, not AI output and not client-approved copy.

## Behavior

- A ready Event can start its V1 primary Campaign. The database permits multiple Campaigns per Event; this package's application path reuses the existing primary Campaign for the Event.
- the operator chooses which content surfaces to prepare: Eventbrite, Meetup, LinkedIn organization, LinkedIn personal, Instagram, and Facebook Groups. These are reviewed text surfaces only. They do not prove account access or authorize any publication. Facebook Groups remain manual; Facebook Page and Event Marketing Portal are not included.
- The generator receives a material Event snapshot and selected surfaces. It only uses available accepted facts and omits unknown values. It does not call OpenAI or another network service, create images, add hashtags, or generate execution payloads.
- A successful initial generation inserts CampaignVersion 1. A supported revision creates a complete successor version. Unsupported or invalid requests create no successor. Historical versions remain read-only and are protected from database `UPDATE` and `DELETE`.
- Supported demonstration revisions identify a surface and a small style operation, including warmer, shorter, more energetic, or less formal wording. A request to change protected Event facts is rejected with a direction to use Event details.
- A version retains the source Event revision and the material Event snapshot used to create its content. Staleness compares the material Event projection; the revision number is provenance and does not itself invalidate a version. Internal notes are outside the material projection.
- Stale campaigns are visibly marked. Copy revisions are blocked until an explicit regeneration creates a successor using current Event facts. Prior versions remain intact.

## Snapshot and hash

The version snapshot stores its schema marker, Event identity/revision and public fact snapshot, selected content surfaces, generated semantic content, and deterministic generator/template identity. Canonical snapshot text is persisted as inserted. A SHA-256 content hash is computed from stable UTF-8 JSON over material Event facts, selected surface order, and content. Database IDs, Event revision, timestamps, and generator metadata do not affect that hash; meaningful fact/content/surface changes do. Selected surfaces are normalized into a fixed order before snapshotting.

`CampaignVersion` rows are append-only by database trigger, not merely by UI convention. `Campaign.current_version_id` points to the current review version; state remains pre-approval `REVIEW`. There is no approval, execution, destination result, or publishing state in this package.

## Boundaries and known limits

The fake service is behind generation and revision protocols so a later marketing model can replace its implementation. Its revision parser intentionally supports a few explicit examples rather than general conversation. Unsupported requests leave current content and history unchanged. Campaign review renders shared Event facts once and shows only content available for selected surfaces; it does not show JSON, payloads, internal identifiers, or hashes.

Real OpenAI marketing generation, image generation, group ranking/selection, platform validation, connectors, approval, simulated execution, and retry/history behavior remain later work. No live destination is contacted and no content is published.
