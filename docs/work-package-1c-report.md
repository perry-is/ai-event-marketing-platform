# Work Package 1C — Real AI Event Extraction

**Status: IMPLEMENTED / ACCEPTANCE PASSED; live OpenAI smoke test deferred.** This package adds real OpenAI-backed event-fact extraction behind the accepted WP1B proposal boundary. It does not add marketing or campaign generation, images, approval, connectors, group recommendations, or publishing.

## Implementation

`OpenAIExtractionService` uses the pinned OpenAI Python SDK `3.24.0` and the Responses API. It is selected only by `EMP_EXTRACTION_MODE=openai`; the model is configured through `OPENAI_EXTRACTION_MODEL` and defaults to `gpt-6-luna`. Each source gets one bounded request, with SDK retries disabled, a 45-second timeout, a 3,000-token output limit, low reasoning effort, no tools, and `store=false`. No automatic model escalation or repeated generation loop is present. A deterministic text-only demo remains the local/test default. Production rejects fake mode and fails startup if OpenAI mode or its server-side key is missing.

The strict JSON Schema output contains at most 30 candidate facts. It constrains field paths to the existing WP1B allowlist, uses `HIGH`/`MEDIUM`/`LOW` confidence, carries a short evidence excerpt and optional uncertainty note, and accepts only a string or the bounded local-schedule object as a candidate. Extra fields and malformed output fail closed. Backend code independently checks source ownership, fields, candidate types, dates, IANA zones, supported enums, title/string limits and HTTPS URLs. The adapter binds each proposal to the source ID in application code; the model cannot assign Event IDs or write database paths. Source-derived candidates remain pending until the existing review/acceptance flow.

The versioned `event-facts-v1` instruction separates application directions from source input. Text is JSON encoded under a single `source_material` key. PDF and image content is a separate request input. Source text and files are explicitly untrusted; instructions quoted inside them cannot supply tools or change the system prompt, schema, authorization or application code path. The request contains only one source and does not include other Events, group/reference data, user/session IDs, provider credentials or application secrets. No chain-of-thought or full raw provider response is requested or persisted.

Pasted text and supported TXT/MD are sent as text. Validated JPEG/PNG/WebP files use inline image data; validated PDFs use inline file data and a generic filename. Both are request-scoped; the provider Files API is not used. The Event Marketing original stays in its existing private storage and is hash/size checked before processing. The reviewed upload policy excludes DOCX/Office files, so WP1C does not accept or add a DOCX parser; the supported text-like upload path is TXT/MD.

## Attempts, validation, and fallback

Extraction attempts use `source_materials.extraction` JSONB; no new table or migration is necessary. A bounded history records attempt ID, Event/source IDs, extractor type, configured model, prompt/schema versions, start/completion times, duration, status, safe failure category/retryability, and successful provider response ID. It stores no key, headers, hidden reasoning, source copy, or unfiltered response. Attempt start commits before network I/O, and the provider call runs without an open database transaction. Results and failures are saved afterward.

Confidence is categorical and advisory. High-confidence proposals without an uncertainty note follow the existing review behavior; medium/low or ambiguous proposals require confirmation. Missing price remains unknown. Existing canonical facts are never overwritten by extraction. New-source date conflicts flow through the existing clarification path. On an explicit retry, still-pending proposals from that same source are superseded and replaced; accepted facts and prior outcomes remain. A process-left-running attempt becomes retryable after three minutes and is recorded as interrupted before retry. Transient timeout, rate limit, and provider-unavailable errors may be retried manually. Refusal, malformed output, invalid source integrity, unsupported input, credential errors, and other non-transient failures do not expose raw details or trigger automatic retries.

An extraction failure preserves the original source and leaves Event editing/clarification available. The UI shows reading/processing states, a plain-language failure/manual-entry message, and a retry button only when the backend marks a retry safe. It does not show model IDs, confidence decimals, raw JSON, token counts, or provider IDs. The deterministic demo can read text only; files stay preserved and are clearly reported as unreadable in that mode. Production requires the OpenAI path.

## Verification

All required automated checks passed:

| Check | Result |
| --- | --- |
| PostgreSQL-backed backend suite | **81 passed** |
| Empty-database migration, repeat upgrade, downgrade, re-upgrade | **Passed** in the backend suite |
| Frontend unit tests | **13 passed** |
| Playwright | **4 passed** across desktop Chrome and iPhone-sized Chromium |
| Frontend Prettier, ESLint/TypeScript, generated API types, production build | **Passed** |
| Python Ruff lint and format check | **Passed** |
| npm dependency audit | **0 vulnerabilities** |
| Python dependency audit (`pip-audit backend --strict`) | **No known vulnerabilities** |
| `git diff --check` | **Passed** |
| Source/reference integrity | **Passed**; four downloaded source hashes and six JPEG hashes remain unchanged |

The backend tests exercise mocked Responses HTTP success, timeout, rate limit, provider unavailability, malformed/incomplete output, refusal, unknown fields, bad value types/enums, key/model configuration, source retention, retry history, stale-attempt recovery, confidence confirmation, per-source conflicts, and inline image/PDF requests. The test fixture set has 11 synthetic scenarios: clean announcement, messy email, missing price, two conflicting sources, virtual and hybrid Events, a generated image input, injection-like text, relative date, and irrelevant text. No the operator source file is used. Browser tests use an HTTP-level OpenAI mock and test-only OIDC substitute; no live provider is contacted. The migration test confirms the complete Alembic chain from an empty PostgreSQL database, then repeat upgrade, downgrade and re-upgrade.

**Live OpenAI smoke test: DEFERRED.** No `OPENAI_API_KEY` was configured in the process environment or local `.env`, and no real credential was requested or added. Live Google OIDC remains deferred as in WP1A. The browser path proves the OpenAI adapter via a mocked HTTP transport, not an actual provider account.

## Security and limitations

The key is a server-only `SecretStr`, accepted from `OPENAI_API_KEY` (or `EMP_OPENAI_API_KEY`), excluded from API models and logs, and blank in `.env.example`. Source data is sent to OpenAI only when OpenAI mode is explicitly configured. Inline file input and `store=false` avoid persistent Files API objects and stored Response retrieval, but do not determine all applicable provider account retention or contractual data-handling terms. The operator must verify those terms before using live client material. A schema-valid but factually mistaken model proposal is still possible; the protection is deterministic validation, provenance, review, conflict handling and the absence of a model-to-canonical-write path—not a claim of perfect model truth.

No architecture conflict was found. There is no new schema migration because operational attempt details fit the reviewed source-material JSON field. PDF/image OCR uses the supported model input path rather than a new OCR stack. DOCX, live API smoke, live OIDC, OpenAI Image API, campaigns, marketing generation, approval, connectors, groups, destination APIs and publishing remain deferred.

## Files changed

- Configuration and app: `.env.example`, `README.md`, `backend/app/config.py`, `backend/app/event_routes.py`, `backend/app/event_service.py`, `backend/app/extraction.py`, `backend/app/file_storage.py`, `backend/app/main.py`, `backend/pyproject.toml`.
- Tests: `backend/tests/browser_server.py`, `backend/tests/test_config.py`, `backend/tests/test_event_intake.py`, `backend/tests/test_foundation.py`, `backend/tests/fixtures/extraction-evaluation.json`, `backend/tests/openai_test_support.py`, `backend/tests/test_extraction_workflow.py`, `backend/tests/test_openai_extraction.py`.
- Frontend: `frontend/src/api.generated.ts`, `frontend/src/events.test.tsx`, `frontend/src/events.tsx`.
- Documentation: `docs/ai-extraction.md`, this report.

No source/reference original changed. No credential, campaign code, new domain table, or migration was added. No push was made.
