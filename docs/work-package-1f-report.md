# Work Package 1F report

**Status: COMPLETE.** Production campaign copy generation and revision now use a server-side OpenAI
Responses adapter behind the existing domain interfaces. The deterministic implementation remains
available for offline development and tests. Approval and simulated destination execution remain
separate, unchanged workflows.

1. **Implementation summary.** Added one-call structured generation, targeted revision, strict
   validation, bounded generation metadata, configuration, browser/provider mocks, tests, and setup
   documentation. No real destination publishing was introduced.
2. **OpenAI campaign adapter.** `OpenAICampaignService` implements the existing generator and
   revision interfaces. The app selects it only when `EMP_CAMPAIGN_GENERATION_MODE=openai`.
3. **Responses API.** Requests use the official Responses API with strict Structured Outputs, no
   tools, `store=false`, a 45-second timeout, no automatic retries, and a 6,500-token output cap.
4. **Model configuration.** `OPENAI_CAMPAIGN_MODEL` is configurable and validated. Production
   fails closed unless campaign mode, model, and the existing server-only OpenAI key are configured.
   No model escalation or fake fallback occurs in production.
5. **Output schema.** The adapter requires the current CampaignContent fields, forbids extra
   properties, requires nulls for unselected surfaces, and accepts a bounded structured factual
   claim echo. Pydantic and selected-surface checks run after the provider response.
6. **Generation prompt/version.** Version IDs are `campaign-generation-v1` and
   `campaign-revision-v1`, independent of the Git commit. Guidance asks for warm, clear, useful
   Event Marketing copy without claiming a verified historical voice.
7. **Locked Event facts.** The current Event snapshot remains application-owned. Only a minimized
   public fact representation is sent; IDs, revision numbers, internal administration, intake
   sources, and source file contents are omitted. The adapter cannot update Event state.
8. **Marketing behavior.** The prompt avoids generic filler, unsupported claims, invented details,
   and fake urgency. Deterministic checks reject mismatched structured facts and detectable date,
   time, URL, price, free/paid, delivery, scarcity, capacity, and outcome conflicts.
9. **Platform-specific content.** Existing shared, Eventbrite, Meetup, LinkedIn organization and
   personal, Instagram, Facebook Groups, and creative-direction fields are used. No platform API
   payloads or IDs are requested.
10. **Revision implementation.** Revision receives the immutable current content, locked facts,
    request, selected surfaces, and allowed field list; it must return a complete successor.
11. **Targeted revision.** Code resolves targets before the provider call and rejects changed fields
    outside that allowlist. Single- and multi-target tests confirm unrelated content stays exactly
    unchanged.
12. **Event-fact protection.** Recognized Event-fact edits are rejected before the provider call.
    Structured factual mismatches and common detectable prose conflicts fail closed. The user is
    directed to update Event details first.
13. **CampaignVersion integration.** Valid generation creates a new immutable version; revision
    creates N+1 and preserves N. Prompt/model/provider response/input-hash metadata is recorded in
    the snapshot, while the existing semantic hash projection is unchanged.
14. **Approval regression.** A test generates and approves an OpenAI-backed Version 1, then revises
    it. The old version and approval remain historical, the successor has no inherited approval,
    and it requires fresh approval.
15. **Execution regression.** AI generation/revision does not create executions or call execution
    operations. The existing simulated execution and approval suites pass unchanged.
16. **Failure handling.** Timeout, rate limit, provider errors, refusal, incomplete/malformed output,
    fact conflicts, and target violations return safe errors. Failed generation/revision leaves the
    Event and existing campaign/approval/history unchanged. The UI gives a retryable plain-language
    message.
17. **Cost controls.** One bounded request per generation, revision, or regeneration; SDK retries,
    automatic repair loops, candidate generation, web search, tool calls, and model escalation are
    disabled.
18. **Backend tests.** **139 passed** against PostgreSQL, including the full WP1A–WP1E regression
    suite and provider/domain tests. Ruff formatting and lint passed.
19. **Frontend tests.** **23 passed.** Prettier check, ESLint, TypeScript build/check, generated API
    type check, and production build passed.
20. **Playwright.** **14 passed** across desktop and iPhone-sized projects. The mocked provider
    exercises the real campaign adapter through generation, revision, approval, simulated execution,
    failure/retry/history, and protected Event-fact correction paths.
21. **Migration result.** The isolated empty-database/repeat-upgrade/downgrade/re-upgrade test passed
    (**1 passed**). WP1F adds no database migration.
22. **Live OpenAI smoke.** **DEFERRED.** The local settings check reported fake campaign mode and
    neither a campaign model nor OpenAI key configured. No real provider call was made. Live
    synthetic verification remains available after private configuration.
23. **Security review.** The key remains server-side. Provider substitution is test-only. Responses
    use `store=false`; no raw provider body or hidden reasoning is stored/logged. Frontend messages
    do not show model IDs or provider diagnostics. Source/reference integrity passed.
24. **Files created/modified.** `.env.example`, `README.md`, `docs/ai-campaign-generation.md`, this
    report, `backend/app/campaign_ai.py`, `campaign_service.py`, `config.py`, `main.py`,
    `backend/tests/browser_server.py`, `test_campaign_ai.py`, `test_campaigns.py`, `test_config.py`,
    `test_foundation.py`, `frontend/src/campaigns.tsx`, and `campaigns.test.tsx`.
25. **Dependencies.** None added. The existing OpenAI SDK and HTTP transport are reused.
26. **Architecture conflicts.** None found. No ORM/table contract, approval rule, state machine,
    hash projection, or execution semantics were redesigned.
27. **Deferred scope.** Live Google sign-in remains governed by its prior WP1A credential setup.
    Live OpenAI smoke is pending private credentials/model configuration. Image generation, Group
    ranking, third-party OAuth/connectors, and live publishing remain out of scope.

The independent source-integrity validator passed for the supplied Event Marketing originals.
Python and npm dependency audits reported no known vulnerabilities; the Python audit skips the
unpublished local application distribution itself. `git diff --check` and the changed-file secret
scan are recorded with the final checkpoint review.
