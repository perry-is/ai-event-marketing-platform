# AI campaign generation and revision

WP1F connects the existing Campaign Generator and Campaign Revision interfaces to the OpenAI
Responses API. The adapter produces review content only. The application remains authoritative for
Event facts, CampaignVersion creation and hashing, approval, destination authorization, execution,
retries, and state transitions.

## Configuration and operating modes

`EMP_CAMPAIGN_GENERATION_MODE=fake` selects the deterministic generator/reviser for tests and offline
development. This is the default outside production. Production configuration requires
`EMP_CAMPAIGN_GENERATION_MODE=openai`, the existing server-only `OPENAI_API_KEY`, and a configured
`OPENAI_CAMPAIGN_MODEL`. The model identifier is configuration, not a code constant. Choose an
account-enabled Luna-class model for normal use; the application does not escalate models or fall
back to fake output.

The OpenAI adapter makes one bounded request for an initial generation, revision, or stale-event
regeneration. It sets a 45-second timeout, disables automatic retries, caps output at 6,500 tokens,
sets `store=false`, and supplies no tools. A person can request another generation or revision after
a failure or if they want different copy. Campaign-generation retries are separate from destination
execution retries.

## Request and output boundary

Initial generation sends a minimized structured representation of the validated Event: title,
schedule, timezone, delivery, and public canonical facts. Event IDs, Event revision numbers, private
administrative data, intake sources, and source-file contents are omitted. A revision also sends the
current campaign content, selected surfaces, the operator's revision request, and the code-resolved list of
fields the model is allowed to change. No connector payloads, platform account identifiers, OAuth
credentials, or generated external URLs are requested.

The Responses request uses strict Structured Outputs with a versioned JSON Schema corresponding to
the existing `CampaignContent` fields. The schema rejects additional fields and requires complete
content, including nulls for unselected destination surfaces. The adapter validates the response
again with the application's strict Pydantic model and surface rules before returning content to the
domain service. Provider response IDs, model ID, prompt/schema versions, and an input hash are stored
as bounded generation metadata in the CampaignVersion snapshot. API keys, raw provider bodies, and
hidden reasoning are never stored or logged. Generation metadata is not part of the semantic
CampaignVersion hash.

`campaign-generation-v3` and `campaign-revision-v3` identify the prompt contracts independently of
Git history. The v3 input includes a server-built canonical fact-claim reference so the model can
copy the exact expected paths and values into `factual_claims`; deterministic validation still
compares that output against the locked Event snapshot. The array is not a summary of copy claims. Copy
guidance asks for warm, energetic, professional, clear, useful writing; prohibits
invented facts and fake urgency; and varies tone by platform only where the audience differs. This
guidance does not claim to reproduce a verified historical Event Marketing voice.

## Fact protection and revisions

The Event snapshot captured by application code remains authoritative and is passed separately from
the generated content. The model cannot update an Event. Its structured factual-claim echo must match
the locked Event facts exactly. Deterministic checks also reject conflicting dates, times, URLs,
prices, free/paid claims, delivery claims, and unsupported numeric scarcity/capacity/outcome claims.
Schema errors, refusals, incomplete output, mismatched claims, and detected prose conflicts fail
closed before the service creates a CampaignVersion. No automatic repair call is made.

The application resolves revision targets before calling the model. For example, “Make LinkedIn
warmer” allows only the selected LinkedIn organization/personal fields; a multi-target request allows
only the named selected surfaces. Unchanged fields must remain exactly equal to the current version.
The adapter rejects a response that changes an unallowed field or changes no field. Requests that
clearly change Event facts are rejected before the provider call and routed back to Event details.
Ambiguous requests remain subject to existing revision validation.

A valid initial result creates a new immutable CampaignVersion through the existing domain service.
A valid revision creates a successor; it never edits the prior version. The adapter does not create
an Approval or touch execution state. A successor of an approved version requires a new approval.
WP1E's simulated execution remains generator-agnostic and unchanged.

## Failures and limits

Provider timeouts, rate limits, connection/status errors, refusals, malformed output, and failed
fact/target checks return a safe generation or revision error. Logs include only an operation and a
coarse failure category. The Event, current CampaignVersion, approval, and execution history remain
unchanged when the call fails. The UI presents a plain failure message and leaves the action available
for another user-requested attempt.

Fact checking is intentionally deterministic and bounded: it catches the explicit structured-fact
echo and common machine-detectable contradictions in copy. It does not claim to prove every sentence
of natural-language marketing copy true. The operator reviews the actual content before approval; future
changes to the protected-fact vocabulary or validators should be tested against the same locked-fact
invariants.

The adapter follows the official [Responses API reference](https://developers.openai.com/api/reference/python/resources/responses)
and [Structured Outputs guidance](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
Automated tests and browser acceptance use a mocked HTTP transport through this real adapter; they do
not contact OpenAI. A live synthetic smoke test is optional when a private key and model are
configured. This package does not generate images, rank Facebook Groups, create platform API
payloads, or publish to external destinations.
