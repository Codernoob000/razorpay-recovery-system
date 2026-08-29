# AI Revenue Recovery Platform

**Razorpay AI Buildathon 2026 — Track 3: AI Revenue Recovery**

An AI-powered agentic pipeline that detects failed payments and subscription renewals, diagnoses why they failed, decides a bounded recovery action, executes it, and measures the result — with a full audit trail for every decision.

---

## The Problem

Merchants lose revenue continuously through the failure funnel: payments degrade, subscriptions fail to renew, and retries are handled inconsistently or not at all. This platform closes that loop end-to-end — detect, diagnose, decide, execute, measure — while staying explainable, bounded, and safe to run unattended.

## Architecture

A five-stage bounded agentic pipeline, each stage its own independently-tested module:

```
Synthetic Transaction Feed
        │
        ▼
┌────────────────┐
│  1. DETECTOR   │  → finds failed transactions + requeues eligible pending_retry ones
└───────┬────────┘     (honors config-driven retry delay windows)
        ▼
┌────────────────┐
│  2. DIAGNOSER  │  → Gemini classifies failure: soft_decline / hard_decline /
└───────┬────────┘     technical_failure / risk_hold, with reasoning
        ▼
┌────────────────┐
│  3. STRATEGIST │  → deterministic rule engine picks a bounded action
└───────┬────────┘     (Gemini used within pre-filtered allowed actions)
        ▼
┌────────────────┐
│  4. EXECUTOR   │  → simulates the action, enforces stopping rules,
└───────┬────────┘     writes Outcome only on terminal states
        ▼
┌────────────────┐
│  5. LEDGER     │  → orchestrates the cycle, full audit trail
└────────────────┘
        │
        ▼
   FastAPI REST layer → 3-screen dashboard (Overview, Audit Trace, Escalations)
```

## Policy Rules (Strategist)

Every decision is bounded by an explicit, testable rule — not left to open-ended LLM judgment:

| Rule | Behavior |
|---|---|
| `RULE_RISK_HOLD_ZERO_RETRY` | Any `risk_hold` classification escalates to a human immediately — zero automated retries, regardless of tier |
| `RULE_MAX_RETRIES_EXCEEDED` | Any transaction at `retry_count >= max_retries` (default 3) escalates immediately, regardless of classification |
| `RULE_HARD_DECLINE_UPDATE_LINK` | Hard declines (expired/blocked cards) route to a payment-method update link |
| `RULE_DISCOUNT_ELIGIBLE_ENTERPRISE` | Enterprise-tier soft declines may receive a discount (up to a configured %) — **once only** |
| `RULE_DISCOUNT_ALREADY_OFFERED` | Guards the rule above — a second discount is never offered to the same transaction |
| `RULE_RETRY_ALLOWED` | Soft declines and technical failures are retried automatically |

**Bounded retry loop:** both the payment-retry path and the update-link path increment a shared `retry_count`, guaranteeing every transaction eventually reaches a terminal state (`recovered`, `escalated`, or `abandoned`) — no transaction can cycle indefinitely.

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| API | FastAPI (fully synchronous) |
| Persistence | SQLModel over SQLite |
| LLM | Gemini 2.5 Flash (`google-genai`, free tier) |
| Resilience | Tenacity retry/backoff |
| Config | Pydantic v2 + pydantic-settings, `config.yaml` |
| Frontend | Static HTML + Tailwind CDN + vanilla JS (no build step) |
| Testing | Pytest — 236 tests |
| Containerization | Docker + docker-compose |
| CI | GitHub Actions (lint + full test suite on push) |

## Getting Started

### Option A — Docker (recommended)

```bash
cp .env.example .env
# edit .env and set GEMINI_API_KEY

docker compose up -d
```

The API and dashboard are now available at `http://localhost:8000`.

### Option B — Local Python

```bash
cp .env.example .env
# edit .env and set GEMINI_API_KEY

pip install ".[dev]"
uvicorn recovery_platform.api.app:app --reload
```

### Seed sample data

```bash
python -m recovery_platform.seed --reset --count 20
```

> **Note on batch size:** the Gemini free tier is rate-limited (~5 requests/minute, 20/day). A batch of 15–20 transactions comfortably fits within the daily quota for a demo run. Seeding significantly larger batches may exhaust the quota mid-run, causing the Diagnoser to fall back to a safe `risk_hold` classification for the remainder of the batch.

### Run a recovery cycle

Click **"Run Recovery Cycle"** on the dashboard (`http://localhost:8000/ui/overview.html`), or:

```bash
curl -X POST http://localhost:8000/pipeline/run
```

## API Reference

Interactive docs available at `http://localhost:8000/docs` once running.

| Endpoint | Description |
|---|---|
| `POST /pipeline/run` | Triggers one full recovery cycle (requeue eligible retries, then process all failed transactions) |
| `GET /metrics` | Recovery rate, ₹ recovered, ₹ at risk, mean time to recovery, escalation count |
| `GET /transactions` | Paginated, filterable list (`status`, `customer_value_tier`, `failure_code`) |
| `GET /transactions/{id}/trace` | Full audit trail for one transaction — diagnosis, action, outcome, exception, with untruncated reasoning |
| `GET /exceptions` | All escalated/unresolved cases, grouped by reason |

## Dashboard

Three screens, served at `/ui`:
- **Overview** (`/ui/overview.html`) — KPIs, status breakdown, transaction table, "Run Recovery Cycle" trigger
- **Audit Trace Inspector** (`/ui/trace.html`) — search any transaction, see its full decision timeline
- **Escalations Queue** (`/ui/escalations.html`) — grouped, actionable list of unresolved cases

## Testing

```bash
pytest -q
```

236 tests across unit tests per module and an end-to-end golden regression suite (`tests/test_golden_batch.py`) that encodes every policy invariant as a standalone, individually-traceable test — so a future change that breaks a rule fails a specifically-named test, not a vague suite-wide regression.

```bash
ruff check .
```

## Known Limitations

- **Gemini free-tier quota** (5 RPM / 20 RPD) constrains demo batch size — see note above.
- **Voice/Hinglish recovery** — deliberately deferred, not part of this build.
- **Checkout drop-off and B2B receivables recovery** — out of scope; this build is intentionally scoped to failed payment/subscription recovery, built deep rather than wide.
- **No real payment gateway integration** — all transactions are synthetic, modeled realistically enough to swap in a real gateway later without a redesign.
- **Schema migrations** — currently uses `SQLModel.metadata.create_all()` on startup rather than versioned Alembic migrations; acceptable for this build's scope, would need addressing for a genuine production deployment against Postgres.

## Project Structure

```
recovery_platform/
├── config.py, database.py, models.py, seed.py
├── api/
│   ├── app.py, schemas.py
│   └── static/          # dashboard (HTML/JS, served at /ui)
└── modules/
    ├── detector.py       # finds + requeues failed transactions
    ├── diagnoser.py       # Gemini-based classification
    ├── strategist.py      # bounded rule engine
    ├── executor.py        # simulated action execution
    ├── ledger.py           # cycle orchestration
    └── llm_client.py       # Gemini client wrapper

tests/                    # 236 tests, incl. golden regression suite
```

---

Built solo for Razorpay AI Buildathon 2026, Track 3: AI Revenue Recovery.
