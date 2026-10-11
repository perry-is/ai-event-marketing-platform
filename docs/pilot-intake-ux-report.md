# Pilot Intake UX & AI Reliability Pass

## Summary

This pass makes Event intake more forgiving for natural date/time descriptions, provides structured date/time and delivery corrections, and makes clarification prompts present human-readable proposals. It adds an AI-assisted event-description suggestion/refinement flow that remains a proposal until the operator accepts it. Campaign-generation failures now have actionable user-facing explanations, and the generation prompt supplies the model an exact server-derived reference for factual claims while preserving deterministic claim validation.

## Intake behavior

- The configured default timezone is `America/Denver`; the suggested default venue is Example Conference Center in Springfield. A suggested venue is not canonical until the user accepts it.
- Deterministic parsing supports common US numeric and month-name dates, years, times/ranges, and common Mountain Time spellings. If the year is omitted, it is inferred from the local date and the proposal explains the interpretation when appropriate.
- Schedule corrections use structured date, start, end, and timezone controls. Delivery is represented by two accessible choices for in-person and online, allowing hybrid to be expressed without a three-way form.
- Clarification proposals display meaningful text rather than serialized internal objects. Accepted corrections continue through authenticated server-side validation and canonical Event updates.

## Description and campaign AI

Event descriptions can be drafted or refined with the configured real text adapter, then accepted or edited manually. Suggestions do not alter canonical Event facts until explicitly accepted. The service sends only the Event context and bounded source-note excerpts needed for the request, and instructs the model to treat notes as untrusted evidence and avoid inventing facts.

Campaign generation uses prompt contract `campaign-generation-v3`. The server derives a locked factual-claim reference from canonical Event facts and includes it in the model request. Existing output/schema and factual-claim validation remain authoritative; model output cannot write canonical Event truth or bypass campaign validation. Generation failures remain non-mutating and do not fall back to fake content.

## AI verification

The local configuration was checked without printing or logging credentials. The configured text model was `gpt-6-luna`; one bounded synthetic event-description request and one synthetic campaign-generation request succeeded against OpenAI. The campaign smoke initially exposed a factual-claim echo mismatch; after adding the server-built exact claim reference, the same bounded workflow passed under the existing validator. No the operator event content was used for these smoke checks. No images were generated automatically; graphics remain available through the user-triggered UI action and the configured real image adapter.

The four external provider modes were verified as `SIMULATED`: Eventbrite, Meetup, LinkedIn Organization, and Instagram Professional. Facebook Groups remain manual. No external publishing requests were made.

## Validation

- Backend: **298 passed** (PostgreSQL-backed suite; migration chain tests included; no schema migration was added).
- Frontend: **61 passed**; lint, type checks/API type generation, formatting, and production build passed.
- Focused Playwright intake flow: **2 passed** (desktop and mobile).
- Full Playwright suite: **58/58 passed** on desktop/mobile after stabilizing the Eventbrite replacement-confirmation test to wait for the refreshed checklist and enabled approval button. The focused Eventbrite spec passed **8/8** on desktop/mobile, followed by the complete **58/58** run. Product approval validation was not changed.
- `npm audit`: **0 vulnerabilities**.
- `pip-audit`: **no known vulnerabilities found**.
- Python Ruff lint/format, frontend lint/type/build/format, and changed-file secret scan: passed.
- `git diff --check`: passed.
- Source integrity: passed; supplied reference originals remain unchanged.

## Scope and remaining limits

No provider connector behavior, approval semantics, or destination execution behavior was changed. OpenAI is used only for configured AI features; distribution remains simulated/manual. The source parser is intentionally deterministic and limited to common date/time patterns; ambiguous dates remain clarification proposals. Live provider OAuth and publishing are outside this pass.
