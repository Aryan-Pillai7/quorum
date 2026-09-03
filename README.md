# Quorum

**AI-augmented three-way payment reconciliation.**
Razorpay AI Buildathon 2026.

Most reconciliation is two-way: does the settlement report agree with the bank? That
question has a blind spot. If the processor says it paid ₹4,50,000 and the bank shows
₹4,50,000 arriving, the pair agrees — and the internal ledger, which booked ₹4,52,000
gross before fees, is nobody's problem until month-end close.

Quorum reconciles three sources against each other:

| Source | What it claims |
|---|---|
| Processor settlement report | what the PSP says it settled |
| Bank statement | what actually reached the account |
| Internal ledger | what our own books recorded |

Two of three agreeing is a lead. Three agreeing is a reconciliation.

---

## Status: Phase 1 (foundation) complete

This repository currently contains the schema, the trust-gating logic, and the service
scaffold. **The matching engine and the AI layer are not built yet.** What runs today:

- ✅ Postgres schema with migrations and a seeded 14-category discrepancy taxonomy
- ✅ Trust-gate policy logic, with tests
- ✅ FastAPI service with health, readiness, and a trust-taxonomy read endpoint
- ✅ Docker Compose stack that comes up clean from a fresh clone
- ⬜ CSV ingestion and the deterministic matching engine (Phase 2)
- ⬜ Claude-backed explanation and classification (Phase 3)
- ⬜ Trust scores learning from real outcomes (Phase 4)

**There are no accuracy or match-rate numbers in this README, because none have been
measured.** Every trust score in the database is a cold-start seed at `sample_size = 0`.
The API says so on every response rather than presenting zeros as scores.

---

## Quick start

### Docker Compose (the reproducible path)

```bash
git clone https://github.com/Aryan-Pillai7/quorum.git
cd quorum
docker compose up --build
```

That starts Postgres, Redis, and the backend, applies migrations, and seeds the
taxonomy. Then:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/ready
curl http://localhost:8000/v1/trust/categories
```

Interactive API docs: <http://localhost:8000/docs>

Postgres publishes on host port **55432** and Redis on **56379**, so the stack does not
collide with local installs.

### Running the backend on the host

```bash
docker compose up -d db cache        # dependencies only

cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# .venv/bin/python -m pip install -e ".[dev]"         # macOS / Linux

cp ../.env.example ../.env           # points at the published host ports
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

No `ANTHROPIC_API_KEY` is needed for Phase 1. Without one the service logs
`agent_enabled=false` at startup and the agent layer stays off.

---

## Tests

```bash
cd backend
.venv/Scripts/python.exe -m pytest -q                 # unit tests, no services needed
.venv/Scripts/python.exe -m pytest -m integration -q  # needs: docker compose up -d db
```

Before pushing (there is no CI — see below):

```bash
.venv/Scripts/python.exe -m ruff check app tests
.venv/Scripts/python.exe -m pytest -q
```

Two of these tests are load-bearing rather than incidental:

- **`test_core_purity.py`** fails the suite if anything under `app/services/matching/`
  imports the AI layer or a network client. The claim that matching is deterministic and
  reproducible is only worth something if something enforces it.
- **`test_config_guard.py::test_create_app_calls_the_guard`** fails if `create_app()`
  stops calling `validate_settings()`. A validation function nobody calls is dead code
  wearing a safety vest.

---

## Layout

```
backend/
  app/
    config.py            settings + the startup validation guard
    core/                money, JSON logging, typed errors — no domain knowledge
    db/                  engine, session, declarative base + naming convention
    models/              SQLAlchemy ORM (how data is stored)
    schemas/             Pydantic (how data is sent) — deliberately separate
    services/
      matching/          deterministic engine (Phase 2) — sealed, no AI imports
      agent/             Claude layer (Phase 3)
      trust.py           the automation gate
      health.py          readiness checks
    api/v1/routes/       thin HTTP adapters
    cache/               Redis — cache only, never a source of truth
  alembic/versions/      hand-authored migrations
  tests/
    unit/                no I/O, no services
    integration/         real Postgres, opt-in via `-m integration`
docker-compose.yml
```

---

## Design decisions worth knowing before reading the code

**Money is integer minor units — `BIGINT` paise, never float.** Reconciliation is
equality comparison on money, so `app/core/money.py` refuses `float` outright and raises
rather than rounding an amount a currency cannot represent. `"100.005"` in INR is an
error, not `100.01`: a silently dropped paisa reappears later as an unexplained
discrepancy.

**The deterministic core is sealed.** Matching produces results from its inputs alone —
no API key, no network, same answer forever. The AI layer explains and proposes; it
never writes ledger state directly.

**The trust gate is a policy ceiling, not just a confidence score.** Every agent
proposal resolves to `AUTO_APPLY`, `HUMAN_REVIEW`, or `BLOCK`. Two rules outrank the
score itself:

- A category marked not auto-resolvable is never automated at any score. Being right 100
  times about `MISSING_IN_BANK` does not make auto-closing the 101st misdirected payout
  acceptable.
- Below `min_sample_size` observations, the score is not evidence. A perfect 1-for-1
  record routes to a human, not to automation.

**Cold start routes to review, never to block.** A category with no history should reach
a person, not halt the pipeline.

**Postgres is the source of truth; Redis is only a cache.** A Redis outage makes Quorum
slower, never wrong and never more permissive. `/health/ready` reports Redis as
`degraded` and still returns 200 — a readiness probe that fails on a cache outage turns
a slowdown into an outage.

**Timestamps are timezone-aware everywhere.** An IST settlement file and a UTC bank
statement 5h30m apart describe the same instant; a naive column turns that into a
phantom date discrepancy. A test asserts no naive timestamp column exists.

---

## Limitations, stated plainly

These are known and deliberate, not oversights.

- **No measured accuracy.** Nothing has been reconciled yet. Any number in this repo is
  a seed value or a threshold, not a result.
- **The audit log is an audit trail, not a tamper-evident one.** `audit_events` is
  append-only by convention and code review. There is no hash chain and no database
  trigger preventing `UPDATE` or `DELETE`. Do not read it as cryptographic provenance.
- **A match record carries one discrepancy category, not many.** Real discrepancies
  often have several simultaneous causes. Phase 2 normalizes this into a one-to-many
  table; Phase 1 keeps a single nullable FK so the taxonomy is not left dangling.
- **Migrations run on container start.** Convenient for a buildathon, wrong for a real
  deployment, where they belong in a separate step.
- **No CI.** Tests run locally, by convention. Nothing mechanically blocks a push with
  failing tests.
- **No authentication.** Every endpoint is open. This is a local-only build.
- **Ingestion is not idempotent yet because ingestion does not exist.** The database
  constraint that will make it idempotent (`uq_transactions_source_external_id`) is in
  place and tested.
- **`min_sample_size` defaults (30/50/100/250 by severity) are judgement calls, not
  calibrated values.** They were chosen so that cold start errs toward human review.
  Phase 4 should revisit them against real outcome data.

---

## Data model

| Table | Purpose |
|---|---|
| `transactions` | one normalized row per source; unique on `(source, external_id)` |
| `match_records` | three nullable transaction legs, the rule that matched them, the delta |
| `discrepancy_categories` | seeded taxonomy of 14 failure modes, with per-category tolerance |
| `trust_scores` | one row per category: score, sample size, thresholds, cold-start floor |
| `audit_events` | append-only record of what changed, and whether a human or an agent did it |

Transactions are never edited to make sources agree. Disagreement is recorded, not erased.
