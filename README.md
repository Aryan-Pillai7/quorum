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

## Status: Phase 4 (aggregated payouts) complete

- ✅ Postgres schema with migrations and a seeded 17-category discrepancy taxonomy
- ✅ Trust-gate policy logic, with tests
- ✅ FastAPI service with health, readiness, and a trust-taxonomy read endpoint
- ✅ Docker Compose stack that comes up clean from a fresh clone
- ✅ CSV ingestion with per-row quarantine and batch tracking
- ✅ Deterministic three-way matching engine with multi-label classification
- ✅ Gemini explanation layer, gated and advisory-only
- ✅ `POST /v1/reconcile` and a dashboard rendering real data
- ✅ Aggregated payouts — N settlement rows explaining one bank credit
- ⬜ Trust scores learning from real outcomes

**Every trust score in the database is still a cold-start seed at `sample_size = 0`.** No
human has confirmed or overridden an agent proposal yet, so nothing has been scored. The
dashboard shows "auto-applied by AI: ₹0.00" — that zero is real, and it is the point.

---

## The 60-second demo

```bash
docker compose up -d db cache
cd backend
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe scripts/seed_demo.py       # 1,468 rows in, reconciled
.venv/Scripts/python.exe scripts/explain_all.py     # 131 findings explained, 9 API calls
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Open <http://localhost:8000/> for the dashboard.

**The story it tells.** Two findings, both explained by the model with self-reported
*high* confidence, both held back — for completely different reasons:

| | `ord_00503` — timing difference | `ord_00480` — duplicate settlement |
|---|---|---|
| What the rules found | bank credited 1 day late, amounts agree | order settled twice, ₹1,657.80 each |
| What the agent says | "standard timing delay within the 2-day window; no action required" | "verify with the processor; if confirmed, initiate a refund for the excess" |
| Model confidence | high | high |
| **Gate decision** | **HUMAN_REVIEW** | **HUMAN_REVIEW** |
| **Why** | eligible for automation in principle, but has **0 of 30** observations — the score is not yet evidence | **never** auto-applied at any score. A policy ceiling, not a confidence judgement |

That contrast is the argument. The model is confident about both. The system automates
neither, and can say precisely why for each — one needs evidence it has not gathered, the
other will never be allowed regardless of how much evidence it gathers.

`GET /v1/narrated` returns these cases as JSON.

**A third case, added in Phase 4** — the engine detecting its own uncertainty:

> A bank credit of ₹1,005.03 matches two different sets of settlement records. It could
> be a single payment, or the combined total of two others. **The system cannot determine
> which grouping is correct**, so it groups nothing and asks for a human.

That is the whole argument in one card: the system knows the difference between an answer
and a guess.

---

---

---

## Phase 4 results — aggregated payouts

Real processors net many payments into a single bank credit. Phase 2 assumed 1:1 and got
this actively wrong: with N settlement rows sharing a payout reference, it matched the
first of them to the credit and reported an amount mismatch on the rest. This phase
removes that assumption for the settlement→bank leg. The ledger→settlement leg is
untouched.

Same `recon_2026_03` fixture, extended with 16 planted payout cases.

| Outcome | Planted | Detected | Notes |
|---|---:|---:|---|
| Shared-reference payouts | 10 | **10** | 43 settlement rows, all correctly assigned |
| Subset-sum payouts | 4 | **4** | 12 settlement rows, all correctly assigned |
| Ambiguous payouts | 2 | **2** | correctly refused — no grouping claimed |
| **Total** | **16** | **16** | 55/55 member rows correctly assigned |

Every Phase 2 category count is unchanged, so nothing regressed. `MISSING_IN_BANK` rises
from 12 to 18 — the six settlement rows inside the two ambiguous payouts stay
deliberately unreconciled, which is what they are.

### The finding worth repeating

**Blind amount matching over an unfiltered pool is not reconciliation, it is coincidence.**

Bounded only by date and currency, the candidate pool for a payout was **87–166 rows** —
past the 40-row search cap, so the search declined to start and resolved **0 of 4**
designed cases. That refusal was correct; the bound was too weak. Excluding settlement
rows whose reference already matches some bank credit (those are matchable 1:1 and have
no business in an aggregation pool) took the pool to **3–13 rows**, and all four
resolved.

The fix was a structural fact about references, not a cleverer algorithm. Two payments of
₹1,200 and one credit of ₹1,200 tells you nothing about which payment arrived.

### Ambiguity is an outcome, not an error

When several distinct sets each sum to the credit, the engine forms no group, claims no
row, records every competing set, and routes to a human. The search stops at the second
solution because uniqueness is the only question worth asking.

This deliberately produces a *lower* match rate. A silently-chosen grouping attributes
money to the wrong payments and looks like a better number while being wrong.

Two settlement rows of equal value count as distinct solutions — which one the credit
covers is exactly what is unknown.

### What aggregation does NOT handle

- **Partial aggregation.** A credit covering *some* of a payout, with the rest arriving
  separately, is not modelled. The group either explains the whole credit or it does not.
- **Cross-currency aggregation.** Candidates are same-currency only. A payout converting
  several currencies into one credit will not resolve.
- **Aggregation combined with another discrepancy.** A payout whose members sum to the
  credit *minus a fee variance* resolves as INCONCLUSIVE, not as "aggregated, with a fee
  problem". The two shapes do not compose.
- **Aggregation across ingestion batches.** Candidates come from the current unmatched
  set; a payout split across two uploads will not group.
- **Pools above 40 candidates are not searched at all.** Reported as INCONCLUSIVE with
  the candidate count, never as "no aggregation exists" — but it is a refusal, not an
  answer.
- **The search bounds are judgement calls**: 3-day window, 40-candidate cap, 50-row group
  cap, 200k node budget. Sized for a laptop, not tuned against production volumes.
- **One cross-cutting rule is not enforced by the schema**: a member's match record must
  have `bank_transaction_id IS NULL`. An integration test asserts it; the type system
  cannot.

---

## Phase 3 results — the agent layer

Measured 2026-09-04 against the same `recon_2026_03` fixture, on the real API.

| | |
|---|---:|
| Findings explained | **131 / 131** |
| Gemini API calls | **9** (one per discrepancy category) |
| Total API time | **51 s** |
| Retries needed | **0** |
| Failed batches | **0** |
| Model | `gemini-3.1-flash-lite` |

Batching by category is what makes that ratio possible: call count scales with the number
of *kinds* of problem, not the number of rows. Per-finding calls would have been 131
requests — see the quota note below for why that matters more than it sounds.

**Every one of those 131 findings is advisory.** Each carries `advisory_only: true`, and
every one passed through the trust gate. `AUTO_APPLY` was returned zero times, because no
category has the observations to earn it.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/reconcile` | match, explain, gate, audit — the whole pipeline |
| `GET /v1/dashboard` | panel data, computed from stored rows |
| `GET /v1/narrated` | the two worked examples above |
| `GET /v1/discrepancies` | findings with explanation and gate decision |
| `GET /v1/trust/categories` | taxonomy with trust scores |
| `GET /health`, `/health/ready` | liveness, readiness |

### What is NOT true of this layer

- **The AI resolves nothing.** It explains and proposes. Nothing it produces is applied,
  and in Phase 3 nothing *can* be: every category is at zero observations, so the gate
  cannot return `AUTO_APPLY` for anything at all. The write path that would apply an
  approved action does not exist yet.
- **Explanations are not verified.** They are grounded structurally — the prompt carries
  only the field comparisons the engine recorded, and nothing else — but no automated
  check confirms a given explanation is factually correct. Validation covers structure
  (right ids, no blanks, within length), not truth. The deterministic finding is shown
  alongside every explanation precisely so a reviewer can check it.
- **`model_confidence` is self-reported and gates nothing.** It is the model's claim about
  its own answer. The trust score is what Quorum has measured. They are deliberately kept
  as separate fields and separate words.
- **The free tier allows 20 requests per day, per model.** Not per minute — that was my
  first wrong assumption from the 429s. At 9 calls per pass, that is two full passes a
  day. Explanations are therefore cached in the audit trail and re-runs skip what is
  already explained; the dashboard reads stored rows and needs no live call. A demo that
  needs a fresh pass on a spent quota will not get one.
- **Latency is not demo-safe without the cache.** A cold full pass takes ~51 s. `POST
  /v1/reconcile` on a warm database returns in well under a second because it skips what
  is explained.
- **The audit trail is append-only by convention only.** No hash chain, no trigger
  blocking `UPDATE` or `DELETE` (ADR-0008). It records every reconcile run, every agent
  batch with its latency and token counts, and every finding's gate decision — but it is
  not tamper-evident and should not be described as such.
- **No authentication on any endpoint,** including the ones that spend API quota.

---

## Phase 2 results

Dataset: **`recon_2026_03`**, checked in at `backend/tests/fixtures/recon_2026_03/`.
Measured 2026-09-04 by `scripts/run_fixture_reconciliation.py`, reproducible with
`pytest -m integration`.

**Read this first: the dataset is synthetic.** It is produced by a seeded generator
(`scripts/generate_fixture.py`) with discrepancies planted by construction. That is what
makes the expected outcomes exactly known — and it is also why these numbers measure
*whether the engine implements its rules*, not whether those rules describe real
settlement files. Real data contains shapes nobody planted. Treat this as a regression
baseline, not as production accuracy. See ADR-0015.

### Ingestion

| Source | CSV rows | Ingested | Quarantined |
|---|---:|---:|---:|
| Processor settlement | 500 | 495 | 5 |
| Bank statement | 492 | 491 | 1 |
| Internal ledger | 483 | 482 | 1 |

All 7 quarantined rows were deliberately malformed, and each was caught with the specific
reason planted: 2 `INVALID_AMOUNT` (one of them `100.005` INR, refused rather than rounded
to paise), 2 `MISSING_REQUIRED_FIELD`, 1 `INVALID_DATE` (`31-02-2026`), 1
`UNSUPPORTED_CURRENCY`, 1 `MALFORMED_ROW` (a bank line with both credit and debit set).
`ingested + quarantined = total` is enforced by a database CHECK, so the denominators
below are not estimates.

### Matching

**Matched 380/492 payment cases cleanly as full three-way matches. 112 carried at least
one discrepancy, spread across 9 categories. 0 fell through to `__novel__`.**

Of 1,468 ingested rows, 503 match records were produced: 380 `FULL`, 82 `BROKEN` (all
three legs present, in disagreement), 41 `PARTIAL` (a leg genuinely absent). Every
ingested transaction belongs to exactly one match record — asserted by a test, because a
row that vanishes between ingestion and matching would silently shrink the denominator.

| Category | Cases | What was planted |
|---|---:|---|
| `TIMING_DIFFERENCE` | 33 | bank credit lagging 1–2 business days |
| `MDR_FEE_VARIANCE` | 28 | processor charged above the contracted rate |
| `MISSING_IN_BANK` | 12 | settled and booked, no money arrived |
| `MISSING_IN_LEDGER` | 10 | settled and credited, never booked |
| `PARTIAL_CAPTURE` | 10 | captured 60% of the authorised amount |
| `MISSING_IN_PSP` | 8 | bank and ledger agree, no settlement row |
| `ROUNDING_DIFFERENCE` | 8 | bank off by ≤ 1.00 INR |
| `ROUTING_SPLIT` | 6 | one order settled across two acquirers |
| `DUPLICATE_ENTRY` | 5 | one order paid out twice in full |

8 of those cases carry **two** categories at once (overcharged *and* credited late) — the
multi-label behaviour that ADR-0012 normalized the schema for.

### What these numbers do NOT cover

Stated here rather than in a footnote, because each is a real gap:

- **Aggregated payouts are not handled at all.** Phase 2 assumes 1:1 settlement — one
  payment, one bank credit. Real processors net forty payments into a single bank line.
  This is the assumption most likely to be wrong against real Razorpay files, and
  supporting it needs a different algorithm (subset-sum with tolerance), not a tweak.
  See ADR-0014.
- **No cross-batch matching.** A payment settled in one file and credited in a file
  ingested later will not match. Every run reconciles what is currently unmatched, with
  no window spanning ingestion boundaries.
- **No partial-match confidence scoring.** Confidence is a fixed value per strategy
  (1.0 exact reference, 0.6 amount+date fallback, 0.3 single leg), not a computed
  likelihood. The numbers are ordinal, not calibrated.
- **Four seeded categories have no rule yet**: `FEE_DEDUCTION`, `TAX_WITHHOLDING`,
  `FX_CONVERSION_DRIFT`, `CHARGEBACK_ADJUSTMENT`. They exist in the taxonomy and would
  currently surface as `AMOUNT_MISMATCH` or `__novel__`.
- **The tolerances and windows are judgement calls, not calibrated values**: 1.00 INR
  rounding, 2.00 INR fee variance, a 2-day timing window, a 3-day fallback pairing
  window. They were chosen to make cold-start behaviour conservative.
- **Multi-currency is untested.** Every fixture row is INR. The money layer supports ten
  currencies and refuses unknown ones, but no cross-currency case has been reconciled.

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

Postgres publishes on host port **55432** (user `quorum`, password `quorum`) and Redis on
**56379**, so the stack does not
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
    static/              dashboard.html, served by the API itself
    services/
      agent/             Gemini layer — prompts, schema, client, batching
      reporting.py       dashboard aggregation
      audit.py           the single write path for audit events
      ingestion/         CSV adapters + batch runner with quarantine
      matching/          deterministic engine — sealed, no AI imports
        rules.py         11 classification rules, pure functions
        engine.py        leg pairing and persistence
      agent/             Claude layer (Phase 3)
      trust.py           the automation gate
      health.py          readiness checks
    api/v1/routes/       thin HTTP adapters
    cache/               Redis — cache only, never a source of truth
  alembic/versions/      hand-authored migrations
  scripts/               fixture generator, fixture reconciliation run
  tests/
    unit/                no I/O, no services
    integration/         real Postgres, opt-in via `-m integration`
    fixtures/            checked-in dataset + its expected-outcome manifest
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
- **Migrations run on container start.** Convenient for a buildathon, wrong for a real
  deployment, where they belong in a separate step.
- **No CI.** Tests run locally, by convention. Nothing mechanically blocks a push with
  failing tests.
- **No authentication.** Every endpoint is open. This is a local-only build.
- **Re-ingesting a file is refused, not merged.** Identical content is rejected on hash;
  a *corrected* file has to go into a fresh batch. There is no amend-in-place path.
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
