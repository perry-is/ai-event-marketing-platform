# Facebook Group recommendations

WP1H implementation contract. Facebook Groups remain **MANUAL ONLY**: the application recommends, the operator decides, and the operator posts. Nothing in this package posts to Facebook, automates a browser, calls a Meta API, or scrapes group pages.

This builds on the reviewed [group model](group-model.md), [Group Master audit](group-field-audit.md), [data model](data-model.md), and [versioning rules](campaign-versioning.md).

## Source workbook and audit assumptions

The only source is an operator-supplied local XLSX export (tests use a synthetic workbook, `backend/tests/sample_group_master.py`). The operator's live Drive is out of bounds.

The audit established:

- 4 worksheets
- 184 group rows
- 182 distinct recorded URLs
- 170 direct group paths
- 12 unresolved share aliases

Those numbers are **not** 184 proven unique Facebook Groups. The workbook also has:

- duplicate and conflicting identities
- name hyperlinks that disagree with the Link column
- contradictory geography
- unnamed columns
- status values under description headers
- rules mixed into admin notes

The importer reproduces the audited counts exactly. Every non-group row is reported as skipped with a reason: header and section rows Team A 2 and 42, and totals rows Team B 41 and Team C 79, 91 and 93.

## Raw evidence vs. normalized Group data

`python -m app.group_catalog <workbook>` is an operator command. It reads the `.xlsx` with the Python standard library (no new dependency) and never writes to the workbook.

### Source material

The workbook is recorded as catalog Source Material. A `source_materials` row with `catalog_kind = FACEBOOK_GROUP_MASTER` has no owning Event or user. It holds:

- the workbook SHA-256
- a private copy of the bytes
- importer version
- tab list
- skipped-row report

### Group records

Each source row becomes one `facebook_group_records` row. Its source evidence is immutable and protected by a database trigger:

- source tab and row number
- a row fingerprint
- `raw_record` with every populated cell (header, exact value, Excel type) and every hyperlink target
- `recorded_urls`, holding the Link cell plus any different name or cell hyperlink

Normalized fields sit alongside the raw evidence and never replace it:

| Field | Rule |
| --- | --- |
| `display_name` | Whitespace-collapsed name. The raw name is kept. |
| `chosen_url`, `group_key` | Only for a `facebook.com/groups/<key>` path. Query, fragment and subpage are dropped; identifier case is kept. |
| `identity_status` | `GROUP_PATH`, `UNRESOLVED_SHARE_ALIAS` or `UNRECOGNIZED_URL`. |
| `geography` | Raw text, recognised US state codes (CO/Co, TX/Tx, AZ/Co), `US` = nationwide. Unrecognised tokens (Penn, WS) are kept as unrecognised. A name that names a different state, or an Ontario name coded CA, is `CONFLICTING`. Nothing is corrected. |
| `member_count` | Whole-number source snapshot, or null. No observation date exists. |
| `audience.category_clue` | Team A unnamed column R only, labelled with its provenance. |
| `admin_notes` | All other populated cells, verbatim, with cell coordinates. They are not interpreted as permissions. |
| `restrictions` | Only explicit source text, with a scope and a cell locator. Examples: `GROUP_ADMINISTRATION` for Team A A67 "DO NOT MAKE ANY CHANGES…", and `MEMBERSHIP_OR_POSTING_RULE` for Team A E51. Only an explicit "do not post / no event promotion" phrase would block event promotion; none exists in the current export. |
| `quality_flags` | `HYPERLINK_MISMATCH`, `GEOGRAPHY_CONFLICT`, `RETENTION_CONFLICT` (Keep vs LEAVE), `OWNERSHIP_NOTE_NOT_MY_GROUP`, `CONFLICTING_DUPLICATE_URL`, `SAME_NAME_DIFFERENT_URL`, `DUPLICATE_SOURCE_ROW`. |
| `review_status` | `UNREVIEWED`, `USABLE`, `CONFLICT` or `EXCLUDED`. This is curated state and is never overwritten by a re-import. |
| `import_status` | `CURRENT`, or `SUPERSEDED` when a later import no longer contains the row. |

## Identity and deduplication

Records are never merged. Identity is only ever derived from a Facebook group path.

| Situation | Treatment |
| --- | --- |
| Same group key (case-insensitive), identical name and geography | The first row is the representative; later rows are `EXCLUDED` as `DUPLICATE_SOURCE_ROW`, linked to it. |
| Same key with a different name or geography (e.g. Team B 4/5, the cross-tab "tea area" rows) | Every row is `CONFLICT`. |
| Same name with different keys (e.g. Omaha Business Network) | Every row is `CONFLICT`. |
| Hyperlink mismatch or Keep/LEAVE retention conflict | The row is `CONFLICT`. |
| Similar names or matching member counts alone | Never treated as identity evidence. |

A `CONFLICT` record cannot be recommended or selected until a human review resolves it. The current export produces nine of them. A review UI is deferred.

## Unresolved share aliases

`facebook.com/share/g/<token>` links do not identify a group. They are stored as `UNRESOLVED_SHARE_ALIAS`, with no chosen URL and no group key, and are always excluded with reason `UNRESOLVED_IDENTITY`. Nothing tries to resolve them.

## Idempotent import and minimum safe synchronisation

- **Same workbook bytes:** returns the existing import (`already_imported`) and creates nothing.
- **Changed bytes:** a new Source Material row is created. Then each row is handled by its fingerprint (tab, cell values, hyperlinks; the row number is excluded so inserted rows don't churn):
  - A row with the same fingerprint as a `CURRENT` record keeps that record, along with its curation and decisions.
  - A changed or new row becomes a new `UNREVIEWED` record.
  - `CURRENT` records not seen in the new import become `SUPERSEDED`.
- Curated `review_status` and reviewer fields are never touched.
- Historical decisions and approvals keep pointing at the records they used.
- A superseded record is no longer eligible. A version or approval that names it is blocked rather than redirected to another group.

## Deterministic eligibility (hard filters)

Every record gets a machine-readable result per recommendation run, persisted on its `group_recommendations` row as `filter_result`:

| Reason | Rule |
| --- | --- |
| `SOURCE_SUPERSEDED` | `import_status != CURRENT` |
| `UNRESOLVED_IDENTITY` | Share alias |
| `UNRECOGNIZED_URL` | No usable group path |
| `IDENTITY_CONFLICT_NEEDS_REVIEW` | `review_status = CONFLICT` |
| `DUPLICATE_SOURCE_ROW` / `CATALOG_EXCLUDED` | `review_status = EXCLUDED` |
| `EVENT_PROMOTION_RESTRICTED` | An explicit source restriction that blocks event promotion |
| `GEOGRAPHY_MISMATCH` | All of: the Event is `IN_PERSON`; its accepted venue/address names a US state; the group has recognised state codes; and none of them match. |

Geography never excludes a group for virtual or hybrid events, when the Event state is unknown, or when the group's geography is unknown or conflicting. Those cases get a caution instead.

Event state comes only from the accepted venue/address text (", CO" or a full state name). If an in-person Event has no state, the run records `EVENT_STATE_UNKNOWN` and the UI says groups weren't filtered by location.

## Event context sent to ranking

Ranking receives only these fields:

- accepted Event title, topic, family label, delivery and venue/address
- host
- the campaign version's `short_summary`

No internal notes, source files, other Events, user or session data, links, raw rows or admin notes are sent.

## Deterministic signals and tiers

| Signal | Points |
| --- | --- |
| `LOCAL_GEOGRAPHY` (state match) | +4 |
| `NATIONAL_AUDIENCE` | +1 |
| `VIRTUAL_EVENT_ANY_LOCATION` | +1 |
| `BUSINESS_COMMUNITY` (business name or "Event Marketing"/"Business" category clue) | +2 |
| `TOPIC_MATCH` (topic/family/title words beyond generic words and state names) | +2 |

| Caution | Effect |
| --- | --- |
| `AUDIENCE_MISMATCH` (buy/sell, housing, photography, spiritual, job audiences) | −3 |
| `GEOGRAPHY_UNCERTAIN` | −1 |
| `POSTING_RULES`, `ADMIN_CHANGE_RESTRICTION`, `OWNERSHIP_NOTE` | Shown only; no score change |

Member count never adds points.

Tiers come from the score:

- ≥ 6: **Best fit**
- ≥ 3: **Good fit**
- ≥ 1: **Possible fit**
- below 1: not shortlisted

The integer score is stored for audit but never shown. Ties are broken alphabetically for determinism.

## AI ranking boundary

The pipeline is: eligible candidates → deterministic sort → at most 30 shortlistable candidates → `GroupRanker.rank(context, candidates)` → validation → persistence.

The candidate payload is limited to:

- an application-generated group ID
- name and geography label
- member snapshot
- category clue
- signal codes
- caution text

### Rankers

- **`OpenAIGroupRanker`**:
  - one Responses API call
  - strict JSON schema `facebook_group_recommendations_v1`
  - `store=False`, no tools, no retries, 30-second timeout
  - model taken from `OPENAI_GROUP_RECOMMENDATION_MODEL` (Luna-class default `gpt-6-luna`, with no automatic escalation)
  - `EMP_GROUP_RECOMMENDATION_MODE=openai` is required in production
- **`DeterministicGroupRanker`**: the same interface, used by development and every automated test. It makes no network calls and has a test-only failure switch.

### Structured output

Each recommendation has:

- `group_id`
- `tier` (`BEST_FIT` / `GOOD_FIT` / `POSSIBLE_FIT`)
- `reason` (at most 240 characters)
- `matching_signals` (enum)
- `caution` (nullable)

At most 12 recommendations are returned.

### Validation

The model cannot select, authorize, or invent groups or facts.

- **Unknown group ID:** the whole result is rejected and the deterministic fallback is used.
- **Duplicate IDs:** the first occurrence is kept.
- **Matching signals:** cut back to the signals the application computed for that candidate.
- **Reasons:** a reason that mentions a number or US state not present in the candidate or Event data is replaced with the deterministic explanation, marked `explanation_source = DETERMINISTIC`.
- **Cautions:** a caution is kept only if it is one the application supplied. Invented rules or "safe for automation" claims are dropped.
- **Persistence:** run metadata records ranker ID, model ID, prompt version, input hash, ranking source and failure category.

### Fallback

If the ranker fails or returns invalid output, the shortlist is ranked deterministically. The run is marked `DETERMINISTIC_FALLBACK`, and the UI explains it.

Manual browsing (`GET …/eligible-groups?q=`) of every eligible group always works. Facebook distribution is never blocked by the model.

## Explanations and shortlist

Each recommendation answers "Why this group?" in one plain sentence. For example: "Colorado audience matches the event location; business community."

There are no confidence decimals. The shortlist holds at most 12 groups; more are available through browsing.

## Human selection (Group Decisions)

Recommendations never select anything. Each checkbox change appends a `group_decisions` row:

- `SELECT` or `REJECT`
- actor, recommendation ID if any
- Event and group revisions
- a new campaign revision

Rows are append-only (trigger). The highest campaign revision per group wins; timestamps never decide ties.

Selecting requires the group to pass the hard filters for this Event. Deselecting is always allowed, and no reason is required.

**Refresh:** running recommendations again creates a new run and never changes decisions. Runs are never refreshed automatically; the page only reads the latest run. A run is shown as stale when the Event revision or any group revision has changed since it was made.

## Approval-binding decision

The reviewed architecture puts selected groups in the **CampaignVersion snapshot**:

- [domain model](domain-model.md): "Latest applicable decisions inform the next version; approved group choices stay frozen"
- [data model](data-model.md): the snapshot contains "exact selected group display/link/rule context and human decision IDs"
- [versioning](campaign-versioning.md): a changed selected group or link is material

So WP1H does this:

1. Decisions are pending intent until a version is assembled. **Save group selection** creates a successor version with the same copy and graphic and the frozen `facebook_group_targets`: record ID, revision, display name, chosen URL, geography label, member snapshot, cautions, decision ID. Every other successor (copy revision, graphic, regeneration) also folds in the latest decisions.
2. The hash projection gains `facebook_group_targets` (record ID, display name, chosen URL) only when groups are present. Versions without groups keep their existing hashes. Explanations, tiers and ranking order are not hashed.
3. Approval is blocked with `GROUP_SELECTION_NOT_SAVED` while decisions differ from the current version. Approval-preview reports `group_selection_pending`.
4. The approved Facebook plan entry carries `selected_groups` and is covered by the immutable approval's plan hash. Approval and execution start both re-check that every group is still current and unconflicted (`GROUP_TARGET_UNAVAILABLE`); a group is never substituted.
5. **After approval**, a decision requires explicit `confirm_withdraw_approval`; otherwise it returns `GROUP_CHANGE_REQUIRES_NEW_APPROVAL`. Confirming:
   - withdraws the active approval through the existing successor path
   - marks pending manual work as superseded
   - leaves the historical Approval and its A/B/C groups untouched (approvals are immutable)

   the operator then saves a successor and approves it. Existing replacement acknowledgements apply if a package was already delivered.

No new approval semantics were introduced.

## Facebook manual package

The single WP1E Facebook Groups execution and its one **Mark Facebook Groups complete** action are unchanged. The package now lists exactly the approved groups, each with an "Open group" link, then the approved graphic (with download), the approved copy and a copy button. Instructions read "Post the approved copy and graphic in each group above…". With no groups selected, the previous "choose groups in Facebook" wording remains.

## Manual-only policy and security

- No Facebook credentials, tokens, OAuth, browser automation, Graph API calls or scraping exist.
- Every group endpoint requires the authenticated session, and mutations need same-origin CSRF.
- The browser cannot forge approved targets, because the plan is built server-side from the immutable snapshot.
- The ranker scenario switch exists only when `EMP_ENVIRONMENT=test`.
- The Group Master stays private: the stored copy is in private storage, and only bounded fields reach the UI or the model.

## Deferred

- A human review UI to resolve `CONFLICT` records and choose links.
- Share-alias resolution.
- Live freshness or enrichment of member counts and rules.
- Learning from the operator's past decisions. Decisions are stored for history only and are not used to rank.
- Per-group execution units with one attestation per group (see the report's architecture conflict).
