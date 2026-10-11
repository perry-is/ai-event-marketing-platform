# AI Event Marketing Platform

A full-stack web application that turns rough event information into a reviewed, multi-channel marketing campaign. It also makes sure nothing gets published that a person did not explicitly approve.

An organizer pastes notes, uploads a flyer, or types a few details. The app extracts proposed facts, asks only about what is missing or conflicting, generates campaign copy and a graphic with AI, and lets the organizer revise until it reads right. Approval freezes one exact version and one exact destination plan. Each destination then runs independently: Eventbrite, Meetup, a LinkedIn organization page, Instagram, and a manual Facebook Groups package. Every outcome is recorded honestly, including the uncertain ones.

> **About this repository.** I built this as contract work for a small business-networking organization. The client's name, data, brand assets, and workbooks have been removed. Tests use synthetic data, and the git history was squashed for publication. See [docs/CASE_STUDY.md](docs/CASE_STUDY.md) for the engineering story.

## What it does

| Stage | Behavior |
| --- | --- |
| **Intake** | Free-form text, notes, images and PDFs are stored privately as immutable originals. AI extraction returns *proposals* with source locators. Nothing becomes a canonical fact until the user accepts it. |
| **Clarification** | Deterministic readiness rules ask only for missing or conflicting facts. Forgiving schedule parsing (omitted years, common US formats, timezone defaults) explains its interpretation instead of silently guessing. |
| **Campaign generation** | An OpenAI Responses adapter with strict structured output writes per-destination copy. Targeted revisions are checked against a server-derived allowlist of fields that may change. |
| **Graphics** | A model generates only the visual layer. Title, date, time and venue are composed locally and deterministically with Pillow, so business-critical text is always exact. |
| **Group recommendations** | A messy spreadsheet catalog of ~200 Facebook Groups is imported with full cell-level provenance, hard-filtered deterministically, ranked by a bounded model call with a deterministic fallback, and selected by a human. |
| **Approval** | Approval binds the exact content hash *and* a separate destination-plan hash to what the user previewed. If anything drifted, the server rejects the approval with `APPROVAL_PLAN_CHANGED`. |
| **Execution** | Per-destination claims, durable attempts, leases, and a conservative result reducer. Ambiguous outcomes become `UNKNOWN` and are never blindly retried. |
| **Credentials** | Delegated OAuth (LinkedIn, Instagram) stored in an AES-GCM credential vault. The encryption is bound to the owning record, and keys can be rotated and resumed. |

## Architecture

```
React + TypeScript (Vite)  ──►  FastAPI  ──►  PostgreSQL (SQLAlchemy 2, Alembic)
                                  │
             ┌────────────────────┼──────────────────────────┐
     Domain services        AI adapters (proposals)     Destination adapters
  (facts, versions,      extraction · copy · graphics   Eventbrite · Meetup ·
   approval, execution)  · group ranking · descriptions  LinkedIn · Instagram ·
                                                          Facebook Groups (manual)
```

Core principle: **models propose and deterministic services decide.** AI output never carries authority. Canonical facts, state transitions, approval and execution are owned by typed domain services, enforced with PostgreSQL locks and immutability triggers.

Detailed docs: [architecture](docs/architecture.md) · [domain model](docs/domain-model.md) · [state machine](docs/state-machine.md) · [execution safety](docs/execution-safety.md) · [mixed-mode approval](docs/mixed-mode-approval.md) · [credential vault](docs/credential-vault.md) · [OAuth connections](docs/oauth-connections.md) · [connector contract](docs/connector-contract.md).

## Engineering highlights

- **Preview-bound approval.** Approval compares a client-supplied expected plan hash against a server rebuild under lock, so a user can never approve a plan different from the one they saw.
- **Content and execution intent hashed separately.** One campaign can mix LIVE, SIMULATED and MANUAL destinations. A later configuration change can never upgrade historical consent into live authority (see migration `0007`).
- **Approval survives execution failure.** Authorization commits in its own transaction before execution initialization, so a startup failure doesn't erase consent.
- **Uncertainty is a first-class outcome.** When a provider times out after a possible create, the result is `UNKNOWN` and the known resource identity is kept, so the attempt can be reconciled against that exact resource instead of resent. Expired execution leases also recover to `UNKNOWN`, not to "retry".
- **Speculative AI generation with conditional commit.** Slow model calls run outside locks. On commit, the captured inputs are re-checked, and stale results are rejected rather than written.
- **Provider capability as an explicit contract.** Each adapter declares the subset it supports and blocks *before* any mutation on anything unverified. For example, Meetup image upload stays blocked until its transport contract is verified.
- **Test mode is structurally undeployable.** Test mode is loopback-only and uses a random per-process vault key, so test scaffolding can't protect real credentials.

## Tech stack

**Backend:** Python 3.11, FastAPI, Pydantic Settings, SQLAlchemy 2, Psycopg 3, Alembic, PostgreSQL 17, Pillow, `cryptography` (AES-GCM), Authlib (Google OIDC)
**Frontend:** React 19, React Router, TypeScript, Vite, CSS modules, generated API types
**AI:** OpenAI Responses API (strict JSON schema output) and Images API, behind replaceable adapters with offline fakes
**Testing:** pytest against disposable PostgreSQL databases with stateful provider HTTP fakes; Vitest + React Testing Library; Playwright (desktop and mobile Chromium)

## Running it

```powershell
Copy-Item .env.example .env
docker compose up -d --wait
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "backend[test]"
.\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini upgrade head
```

All AI and destination integrations default to offline fake or simulated modes, so no API keys are needed to run the tests. See [docs/development.md](docs/development.md) for full setup, test commands, and live-connector configuration.

## Status and honest limits

The full workflow is implemented and tested locally against provider fakes. Not yet verified: publishing against real provider accounts, production deployment, backup/restore exercises, and PWA installability. Facebook Group posting is intentionally manual. The [case study](docs/CASE_STUDY.md) lists the known open issues.
