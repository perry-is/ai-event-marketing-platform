# Facebook Group candidates and human decisions

WP0B proposal based on [Group Master audit](group-field-audit.md), GRP-003–011 in [evidence](evidence/observations.md). **CONFIRMED export evidence:** 184 source rows, 182 recorded URL keys, 170 direct keys and 12 unresolved share aliases; duplicate identity conflicts, mismatched hyperlinks, ambiguous geography, unnamed columns and rules mixed into admin notes. **UNKNOWN:** true identity count, current reach, live rules/account permissions. Facebook Groups are **MANUAL ONLY**.

## Practical representation

Each source row becomes a local `facebook_group_records` candidate, identified by application UUID. Preserve source file hash/id, exact tab/row, positional/header raw values, Link-cell URL and any different name hyperlink. Unique import coordinates prevent repeated import of that exact row; raw URL/name has no identity uniqueness constraint. Do not merge same-name/different-link records or unresolved share URLs automatically.

Curated fields are display name (trimmed presentation, original retained); recorded URLs with origin/conflict metadata; chosen URL nullable; geography components and confidence; audience/category clues; member snapshot/count and observed date nullable; admin/ownership notes; scoped restrictions; narrative notes; review status UNREVIEWED/USABLE/CONFLICT/EXCLUDED; user reviewer/time and revision. USABLE means reviewed local manual candidate, not guaranteed posting permission. No boolean `is_admin` derived from Done/Yes/Me.

Member counts are numeric source snapshots; absence of observation date is visible. “Group Description” contains mostly status values, so it is preserved as raw source content, not treated as an audience description. Curated restrictions carry action scope (membership, moderation, event promotion, banner/name changes, unknown), evidence locator and certainty; a model may propose this classification but a human confirms binding exclusions. Keep/LEAVE conflict is CONFLICT. Unknown geography remains unknown; no silent correction from group name.

Human review can choose a reliable link/name for one candidate, exclude a record, or confirm two records represent the same target. Do not build global Facebook identity resolution. V1 prevents selecting known same analytical group-path keys twice; flagged same-name/conflicting links/share aliases require targeted review before selection. Keep both original rows; explicitly mark any duplicate excluded with link to the reviewed representative in audit metadata. Arbitrary name similarity is a warning, not a merge rule.

## Recommendation pipeline

1. **Deterministic filtering:** use confirmed eligibility/exclusion, confirmed geography/category requirements and explicit event-promotion restrictions. Invalid/unavailable/conflicted target cannot be selected. Unknown fields do not mean eligible or ineligible by default; show review-needed candidates separately. Never interpret membership/no-banner rules as blanket event-promotion rules.
2. **Model ranking/reasoning:** among candidates permitted for consideration, rank relevance to accepted Event facts using names, audience clues and notes. Persist run/input hash, Event/group revisions, filter result, explanation and model metadata. Score is advisory, not a probability/permission claim; membership size alone is insufficient.
3. **Human selection:** the operator sees name, chosen/conflicting links, geography, snapshot count/freshness, audience/reason and relevant restrictions. Select/reject appends a Group Decision; it does not post. A recommendation cannot write a SELECT decision. User reviews only needed conflicts, not all 184 rows as an intake requirement.

Recommendations become stale when input facts or curated group revision changes. Latest selections are revalidated during version assembly and frozen into snapshot with chosen URL/display/rule context and decision reference. A group-choice/link change after approval is material. If a catalog record later becomes excluded/conflicted, pending manual delivery is blocked; no silent redirect to another group. Prior approved packages/history remain available as superseded evidence with warnings.

Facebook Groups are one manual destination package containing the approval-bound selected target set. The operator completes that package with one manual completion action; the UI presents the selected groups together with the approved copy and graphic. No per-group execution or attestation is required. No scraping, automated posting, membership requests, moderation, rename, banner change or live resolution is authorized.

Learning from the operator's decisions is deferred. V1 stores decisions for explanation/history, not automatic future selection, inferred default routes or training uploads. Confirm a reusable routing/default policy explicitly before adding one.
