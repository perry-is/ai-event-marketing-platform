# Work Package 1H report

**Status: COMPLETE**, with the optional live ranking smoke test DEFERRED (no OpenAI key configured).

Baseline: clean tree at `499ab8e74262f7bfbd2448b0bccc2c2498298d73`. Source integrity passed before and after implementation.

Facebook Groups remain **manual only**. There is no automated posting, browser automation, Meta API, scraping, OAuth or any other live connector. The full contract is in [Facebook Group recommendations](facebook-group-recommendations.md).

1. **Implementation summary.**
   - The local Group Master is imported as immutable row evidence with conservative normalization.
   - Deterministic hard filters (each with a machine-readable reason) and signals/tiers produce a bounded shortlist.
   - A replaceable ranker (OpenAI Responses with a strict schema, or a deterministic fake) orders and explains that shortlist; its output is validated.
   - the operator selects groups through append-only decisions.
   - "Save group selection" freezes the selection into a successor CampaignVersion snapshot; approval binds to it.
   - The single manual Facebook package shows exactly the approved groups, graphic and copy.

2. **Source workbook import.**
   - `python -m app.group_catalog <xlsx>` reads the workbook with the standard library only.
   - It finds 184 group rows: Team A 63, Team B 39, Team C 76, Available 6.
   - Six non-group rows are reported as skipped with a reason.
   - Original bytes and SHA-256 are unchanged; a test asserts this.
   - A private copy is stored under catalog Source Material.

3. **Raw provenance.**
   - Each row keeps its tab, row number, every populated cell (header, exact value, type), all hyperlink targets, and a fingerprint.
   - A PostgreSQL trigger rejects edits to that evidence and deletion of group records.

4. **Normalization.**
   - Only reliable transforms are applied: whitespace, group-path URLs, recognised state codes, `US` = nationwide, numeric member snapshots, search tokens, and explicit restriction text with cell locators.
   - Penn, WS, and name/state contradictions are flagged, never corrected.
   - Status and admin columns are preserved verbatim and not interpreted as permissions.

5. **Identity and deduplication.**
   - Nothing is merged.
   - The same key with an identical name and geography counts as a duplicate: the later row is EXCLUDED, linked to the first.
   - The same key with different details, the same name with different keys, hyperlink mismatches and Keep/LEAVE conflicts are all CONFLICT and need review.
   - Nine CONFLICT records in the current export, including Team B 4/5, Team A 22, Team B 29 and both Omaha rows.

6. **Unresolved aliases.** All 12 share links are `UNRESOLVED_SHARE_ALIAS`, with no chosen URL or key, and are always excluded as `UNRESOLVED_IDENTITY`.

7. **Eligibility filters.**
   - `SOURCE_SUPERSEDED`
   - `UNRESOLVED_IDENTITY`
   - `UNRECOGNIZED_URL`
   - `IDENTITY_CONFLICT_NEEDS_REVIEW`
   - `DUPLICATE_SOURCE_ROW` / `CATALOG_EXCLUDED`
   - `EVENT_PROMOTION_RESTRICTED`
   - `GEOGRAPHY_MISMATCH`: in-person Event with a known state against a group with recognised, different states.

   Virtual, hybrid, unknown-state and unclear-geography cases are not excluded. Every run persists a filter result for every record.

8. **Deterministic scoring.**
   - Small integer signals: local +4, national +1, virtual +1, business community +2, topic +2.
   - Penalties: audience mismatch −3, unclear geography −1.
   - Tiers: Best fit (≥ 6), Good fit (≥ 3), Possible fit (≥ 1).
   - Member count never adds points, and scores are never shown.

9. **AI ranker architecture.**
   - The `GroupRanker` protocol has two implementations: `OpenAIGroupRanker` and `DeterministicGroupRanker`.
   - The ranker receives at most 30 pre-filtered candidates.
   - Payload: group ID, name, geography, member snapshot, category clue, signals and cautions, plus accepted Event facts and the campaign summary.
   - Excluded from the payload: links, raw rows, admin notes, sources, sessions and other Events.

10. **Model configuration.**
    - `EMP_GROUP_RECOMMENDATION_MODE=fake|openai`; production requires `openai`.
    - `OPENAI_GROUP_RECOMMENDATION_MODEL` defaults to the Luna-class `gpt-6-luna`; there is no automatic escalation.
    - Uses the existing server-only `OPENAI_API_KEY`.
    - One call per run: no retries, no tools, `store=False`, 30-second timeout.

11. **Structured output schema.** Strict `facebook_group_recommendations_v1`, at most 12 recommendations, each with:
    - `group_id`
    - `tier` (enum)
    - `reason` (≤ 240 characters)
    - `matching_signals` (enum)
    - `caution` (nullable)

12. **Explanations.**
    - One plain sentence answering "Why this group?".
    - Unknown IDs reject the whole result, which falls back to deterministic ranking.
    - Duplicate IDs keep the first occurrence.
    - Signals are cut back to the computed ones.
    - Reasons citing unsupported numbers or states are replaced with deterministic text.
    - Cautions must be ones the application supplied.

13. **Human selection.**
    - Each checkbox change is an append-only `group_decisions` row with the actor, Event, group and campaign revisions, and recommendation ID.
    - The highest campaign revision wins.
    - Selection requires eligibility; deselection is always allowed and needs no reason.
    - Recommendations never select anything.

14. **Approval-binding decision.**
    - **CampaignVersion snapshot**, per the reviewed domain, data and versioning models. A changed group or link is material.
    - **Save group selection** creates a successor that carries the same copy and graphic, plus the frozen `facebook_group_targets` (with decision IDs). All other successors also fold in the latest decisions.
    - The hash includes group ID, name and URL only when groups exist, so earlier hashes are unchanged.
    - Approval is blocked while decisions are unsaved (`GROUP_SELECTION_NOT_SAVED`).
    - After approval, a change needs explicit confirmation and withdraws the active approval. The historical Approval keeps its exact plan, A/B/C.
    - A successor plus fresh approval is required. No new approval semantics were introduced.

15. **Recommendation refresh.**
    - Refreshing on request creates a new, append-only run; nothing refreshes on page load.
    - Selections are untouched by a refresh.
    - A run is marked stale when Event or group revisions change.
    - Group records are never duplicated.

16. **AI failure fallback.**
    - On a provider error, invalid output or unknown ID, the shortlist is ranked deterministically and marked `DETERMINISTIC_FALLBACK`, with an explanation in the UI.
    - Manual browsing and search of every eligible group always work.
    - Campaign review, approval and the Facebook package are never blocked by the model.

17. **Facebook manual-package integration.** The plan entry and package list exactly the approved groups (name, link, geography, member snapshot, cautions). The UI order is:

    1. Where to share, with an "Open group" link for each approved group
    2. Graphic, with download
    3. Approved copy, with a Copy button
    4. One **Mark Facebook Groups complete**

    There is no per-group completion. Approval and execution start re-check that the groups are still available and never substitute another.

18. **Security and manual-only safeguards.**
    - All group APIs require a session (anonymous requests get 401) and CSRF on mutations (403 is tested).
    - Plans are built server-side from immutable snapshots.
    - Approvals and decision/recommendation history are immutable or append-only at the database level.
    - The test ranker switch only exists when `environment == "test"`.
    - No credentials or posting code.
    - Group Master data stays in private storage and authenticated views.

19. **Backend tests.** Full PostgreSQL suite: **169 passed, 0 failed**. That is the 154 existing tests (with fixture, config and table-list updates) plus 15 new ones in `tests/test_facebook_groups.py`:
    - real workbook import, provenance and unchanged original
    - idempotent repeat import
    - changed source with curation preserved and rows superseded
    - malformed and ambiguous rows, and the raw-evidence trigger
    - hard-filter reasons
    - size not counted as relevance
    - auth and CSRF
    - a bounded, explained run with persisted exclusions
    - the strict OpenAI request with ID, duplicate, fact and caution validation
    - fallback plus browsing
    - selection persistence across refresh, and eligibility enforcement
    - approval binding, expansion protection, withdrawal and an unchanged historical approval
    - the manual package with exact groups, graphic and copy, completed once
    - hash compatibility
    - test controls absent outside test mode

20. **Frontend tests.** **37 passed, 0 failed**: 28 existing plus 9 new. They cover:
    - the suggestions empty state without auto-load
    - tiers, reasons, members, cautions and links
    - select/deselect decisions, the count, and the save
    - fallback notice and browse/search
    - the approved lock with confirmation before a change
    - historical groups read-only
    - unavailable suggestions without an alert
    - accessible link names
    - the approval summary with a pending warning
    - the manual package listing exactly the approved groups

    Lint (0 warnings), type checks, Prettier and the production build passed.

21. **Playwright.** **20 passed, 0 failed**: 10 tests each on desktop Chromium and iPhone 13, including two new WP1H tests per project.
    - **Flows A–C:** recommend, select 4, deselect 1, persist across reload, save → Version 3; approve, start, confirm the package shows exactly 3 groups with the approved graphic and copy, mark complete once; add a 4th after approval, so the active approval is withdrawn, the historical approval keeps 3, and Version 4 needs approval. No horizontal overflow.
    - **Flow D:** simulated ranker failure falls back, and browse/search selection still works.

    The test backend imports the local Group Master copy read-only into a disposable PostgreSQL database.

22. **Migration.** `0006_facebook_groups`:
    - makes catalog Source Material ownerless, with a constraint and a unique catalog hash
    - adds `facebook_group_records` (source-evidence trigger), `group_recommendations` and `group_decisions` (append-only triggers)

    The foundation migration test passed: empty database → head, `alembic check` (models match), downgrade to base, re-upgrade. No Facebook credential or token tables were added.

23. **Live OpenAI smoke test.** **DEFERRED.** `OPENAI_API_KEY` is not configured locally, so no live call was made. The strict request shape and validation are covered with a mocked HTTP transport.

24. **Source integrity.** `scripts/verify-source-audit.py` **PASS** before and after. The workbook, the other downloads and the six JPEGs are unchanged.

25. **Files.**
    - **New:**
      - backend: `app/group_catalog.py`, `app/group_ranker.py`, `app/group_recommendations.py`, `app/group_routes.py`, `migrations/versions/0006_facebook_group_recommendations.py`, `tests/test_facebook_groups.py`
      - frontend: `src/campaignApi.ts` (extracted API helper), `src/facebookGroups.tsx`, `src/facebookGroups.test.tsx`, `e2e/campaign-groups.spec.ts`
      - docs: `docs/facebook-group-recommendations.md` and this report
    - **Modified:**
      - backend: `app/campaign_service.py`, `app/config.py`, `app/execution_service.py`, `app/main.py`, `app/models.py`, `app/simulated_connectors.py`, `tests/browser_server.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_foundation.py`
      - frontend: `src/App.module.css`, `src/api.generated.ts` (additive only: +259 lines), `src/campaigns.tsx`, `src/campaigns.test.tsx`
      - root: `README.md`, `.env.example`

26. **Dependencies.** None added. npm audit found **0 vulnerabilities**; pip-audit found **no known vulnerabilities** (local editable package skipped).

27. **Resolved versioning and manual-execution semantics.** Facebook Groups are one manual destination package containing the exact approval-bound selected target set, completed once with **Mark Facebook Groups complete**; there are no per-group execution records or attestations. A copy-only CampaignVersion revision preserves the same graphic asset unless the graphic itself is revised. The focused successor-inheritance patch centralizes that behavior while leaving graphic-revision and approval rules intact.

28. **Explicitly deferred.**
    - human review UI for CONFLICT records and link choice
    - share-alias resolution
    - live group freshness or enrichment
    - learning from past decisions
    - live destination integrations, OAuth and automated Facebook posting (permanently out of scope for Groups)
