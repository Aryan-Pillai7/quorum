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

## Status: Phase 8 (trust decay on silence) complete

- ✅ Postgres schema with migrations and a seeded 17-category discrepancy taxonomy
- ✅ Trust-gate policy logic, with tests
- ✅ FastAPI service with health, readiness, and a trust-taxonomy read endpoint
- ✅ Docker Compose stack that comes up clean from a fresh clone
- ✅ CSV ingestion with per-row quarantine and batch tracking
- ✅ Deterministic three-way matching engine with multi-label classification
- ✅ Gemini explanation layer, gated and advisory-only
- ✅ `POST /v1/reconcile` and a dashboard rendering real data
- ✅ Aggregated payouts — N settlement rows explaining one bank credit
- ✅ Hash-chained audit trail, safe cache invalidation, CI
- ✅ Approval loop — audited human feedback moves trust scores
- ✅ Category drill-down — the math and the narrative, side by side
- ✅ Trust decays when audits stop arriving
- ⬜ Maintenance-mode override, drift-spike detection

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

---

---

---

---

## Phase 8 results — trust decay on silence

Phase 6 made trust move on audited feedback. Nothing reacted to feedback *stopping*, so a
category that earned `AUTO_APPLY` kept it indefinitely on evidence that could be months
old. Trust is a claim about how often the system is *currently* right, and that claim
weakens with age whether or not anything writes it down.

`TIMING_DIFFERENCE`, holding a real earned score of 0.9988 over 30 audited observations:

| Days since last audit | Effective score | Gate |
|---:|---:|---|
| 0–14 | 0.9988 | `AUTO_APPLY` |
| 21 | 0.8741 | **`HUMAN_REVIEW`** |
| 28 | 0.7494 | `HUMAN_REVIEW` |
| 42+ | 0.5000 (floor) | `HUMAN_REVIEW` |

14 days grace, then 28 days to the floor. Automation is lost about three weeks after the
last audit; the score bottoms out at six.

### Why read-time, not a scheduled job

**A cron that silently stops running leaves every score stale-high** — failing toward
permissive, the one direction a trust system must never fail. Read-time decay cannot get
stuck: if a score can be read at all, its discount has been applied, because computing it
*is* reading it.

It also never mutates the stored score. The record of what the audits actually found stays
intact, and a single fresh audit restores automation by moving one timestamp.

### Three properties that hold by construction

- **Decay only ever pulls down.** Silence is not evidence of correctness, so it can never
  make a category more automatable than its audits justify.
- **The floor is the review threshold, not zero.** Losing automation is the point; halting
  the pipeline is not. Decaying to `BLOCK` would stop surfacing suggestions entirely.
- **A category with no audited observations does not decay.** Cold start and gone-quiet are
  different situations; conflating them would report every new category as degrading.

Decay composes *underneath* the policy ceiling and the high-value fail-safe (ADR-0027),
both of which short-circuit before the score is consulted — so a discounted score cannot
make either more permissive.

### Visible, not silent

The category table shows days since last audit, flags decaying categories, and shows the
discounted score next to the struck-through earned one. A decaying category appears **even
with no open findings** — going quiet usually means no findings arrived either, and hiding
it would defeat the point for exactly the categories most worth noticing.

---

## Phase 7 results — category drill-down

Click any category row on the dashboard to see the individual findings behind the number.

Each finding shows **the field comparison the rules actually performed** on the left and
**what the AI said about it** on the right. The point of putting them side by side is that
you can check the narrative against the arithmetic without leaving the row.

```
ord_00498   R06_mdr_fee_variance          Psp fee ₹176.29  Variance ₹58.76   [Show explanation]
┌──────────────────────────────────────┬──────────────────────────────────────┐
│ WHAT THE RULES COMPUTED              │ WHAT THE AI SAID ABOUT IT            │
│ psp.fee_minor vs fee implied by      │ The processor deducted INR 176.29    │
│ the ledger rate                      │ in fees, while the ledger expected   │
│   Psp fee            ₹176.29         │ INR 117.53...                        │
│   Expected fee       ₹117.53         │                                      │
│   Variance            ₹58.76         │ gemini-3.1-flash-lite · confidence   │
│   Tolerance            ₹2.00         │ high · advisory only                 │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

### Revealing costs nothing; generating is batched

Explanations were generated by an earlier Phase 3 batch and live in the audit trail, so
opening a category and expanding a finding **makes no model call at all**. A test asserts
this by replacing the model client with one that raises if constructed, then loading the
view.

Where a finding has no stored explanation — it never went through a batch, or its batch
failed under ADR-0018's per-category isolation — the view shows *"Not yet explained"* with
a visually distinct generate action. That action **re-runs the existing Phase 3 service**
with a category filter. There is no per-finding call path anywhere in the system.

Verified against real Gemini on a scratch database: 12 `ROUTING_SPLIT` findings, **1 API
call**, 12 explanations written to Postgres, 0 other categories touched. Pressing it again
costs nothing — already-explained findings are skipped.

The generate action requires the operator token, because spending daily API quota is an
operational cost rather than a read. Reads stay open.

### Notes

- Explanations ship inline with the findings list, decided on measurement: 34.8 KB with
  them versus 27.3 KB without, so a second round-trip would save 7.5 KB and add a spinner
  to an action whose whole point is that the answer is already there (ADR-0029).
- The endpoint caps at 500 findings. At roughly 1 KB each, a category with thousands of
  findings would need pagination before this decision holds.
- Deterministic evidence is flattened into label/value pairs server-side, so the UI renders
  any category — including ones added later — without knowing anything about them.

---

## Phase 6 results — the approval loop, closed

Until now the system explained and gated but nothing could act, and no trust score had ever
moved. This closes that loop.

### A category earning automation, end to end

```bash
export OPERATOR_TOKEN=demo-secret
AUDIT_BASELINE_RATE=1.0 python scripts/demo_trust_movement.py TIMING_DIFFERENCE
```

```
BEFORE   TIMING_DIFFERENCE          AFTER    TIMING_DIFFERENCE
  gate         : HUMAN_REVIEW         gate         : AUTO_APPLY
  score        : 0.0000               score        : 0.9988
  observations : 0 of 30              observations : 30 of 30
```

The gate flips at observation **30** — and the 30th approval's stored selection reason
records why it was guaranteed an audit: *"auditing this approval could move
TIMING_DIFFERENCE out of HUMAN_REVIEW"*. Every figure is read back from Postgres, not from
an API response.

**30 is not a lowered demo threshold.** It is `TIMING_DIFFERENCE`'s seeded
`min_sample_size` for a LOW-severity category, unchanged since Phase 1. The gate opens
where it would open in production.

### Approving is not evidence — auditing is

Two separate acts, by two people (ADR-0025):

| | Moves trust? |
|---|---|
| **Approve** a drafted correction — clears the finding operationally | ❌ no |
| **Audit** that approval — a second person confirms it was right | ✅ yes |

Counting approvals directly would measure how often reviewers click accept, which tracks
queue pressure at least as well as correctness — a score built that way rises fastest
exactly when reviewers are most overloaded.

What gets audited is not left to chance where it matters: **any approval that could change
what a category is allowed to do is audited at 100%**, decided by simulating the gate both
ways. The rest are sampled, drawn from a hash of the approval id so a caller cannot retry
until it escapes the sample.

### The fail-safe that outranks earned trust

`TIMING_DIFFERENCE` is now at `AUTO_APPLY`. A transaction in it above **₹2,000** still
returns `HUMAN_REVIEW` (ADR-0027).

Trust is measured per *category* — how often the system is right about a kind of problem.
It says nothing about what one mistake would cost. A category at 0.9999 over ten thousand
observations is still wrong once in ten thousand, and that one should not be a large payout
closing itself.

### What was adjusted for the demo, and what was not

The **audit baseline rate** is raised to 1.0 for the walkthrough. At the default 20% the
loop still works — the first run moved the score 0 → 0.5904 on four sampled audits — but
reaching 30 audited observations needs roughly 150 approvals against 33 available findings.
Auditing everything is strictly *more* evidence per approval: the sample exists to save
human effort, not to add rigour.

### Auth is demo-grade, and that is not a euphemism

One shared bearer token on the two write endpoints (ADR-0028). Reads stay open.

**No users, no roles, no expiry, no revocation, no per-approver identity** — `approver_id`
is self-asserted in the request body and the token does not verify it. Anyone with the
token can claim to be anyone. It fails closed: with no token configured the endpoints
return 503, never 200.

### Still open, deliberately

Maintenance-mode override, drift-spike detection, and feedback-backlog decay are **not
built**. A category that degrades will be caught by the EMA within a handful of audited
observations, but nothing detects a sudden spike faster than that, and nothing decays trust
when feedback simply stops arriving.

---

## Phase 5 results — hardening

No new matching behaviour. Three safety gaps earlier ADRs left open, closed.

### Tamper-evident audit trail (ADR-0022, extends ADR-0008)

Every audit entry now stores `sha256(its own content || the previous entry's hash)`.

```bash
python scripts/verify_audit_chain.py    # exits 1 if the chain is broken
```

| Tampering | Detected | How |
|---|---|---|
| A stored row is edited | ✅ | its hash no longer matches its content |
| A row is deleted from the middle | ✅ | the linkage breaks — each surviving row is still internally valid |
| A row is inserted or reordered | ✅ | same linkage break |
| The whole chain is recomputed after an edit | ❌ | **passes verification** — see below |

That last row is the honest one, and there is a test that performs the attack and asserts
it succeeds. The chain detects *silent* alteration. It does not stop an operator with
write access. Closing that properly needs entries signed with a key the database server
does not hold, or digests anchored somewhere the operator does not control — both real
options, neither in scope here.

### Trust cache: gating never reads it (ADR-0023, corrects ADR-0009)

ADR-0009 argued a failed invalidation was safe because reads fall back to Postgres. That
covers Redis being **down**. Writing the failure tests surfaced the case it does not
cover: a `DELETE` that fails while `GET` keeps working. That is not a miss, so nothing
falls back — the cache serves the stale permissive score, fast and confident.

So the guarantee is now structural rather than argued: **gating reads Postgres directly
and never touches the cache.** The cached read exists only for display. An import-graph
test fails the build if any gating module reaches for the cache.

The load-bearing test makes invalidation fail while reads still work, poisons the
surviving key with a mature 0.99 score over 5,000 observations, confirms the cached read
really is poisoned, and asserts the gate still returns `HUMAN_REVIEW` from the one real
observation in Postgres.

Updates write Postgres and commit **before** deleting the key. Inverting that lets a
concurrent read repopulate the key from the pre-update row.

### CI (ADR-0024, supersedes ADR-0010)

One workflow: ruff, unit tests, integration tests against Postgres and Redis containers,
and a check that the fixture regenerates byte-identically. That last step immediately
earned its place — `csv.writer` defaults to CRLF, so the fixture's bytes had been
depending on which OS generated it. ADR-0015's reproducibility claim was accidentally
true on one machine; now it is pinned to LF and actually true.

**Totals: 220 unit tests, 59 integration tests, ruff clean.**

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
- **The audit log is tamper-*evident*, not tamper-*proof*.** Every entry is hash-chained
  to its predecessor, so an edit, a deletion, or an insertion is **detected** by
  `python scripts/verify_audit_chain.py`. It is **not prevented**: anyone with database
  write access can alter a row and recompute every hash after it, and verification will
  then pass. A test performs exactly that attack and asserts it succeeds, so this limit
  is demonstrated rather than merely disclaimed. There is no signing key and no external
  anchor. Rows written before Phase 5 have no hashes and are reported as predating the
  chain rather than counted as verified.
- **Migrations run on container start.** Convenient for a buildathon, wrong for a real
  deployment, where they belong in a separate step.
- **CI runs lint plus the full suite on every push**, against real Postgres and Redis
  service containers, with no API key set — so the suite must pass with the agent layer
  disabled. It does not deploy anything, and it does not run the Gemini path.
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
