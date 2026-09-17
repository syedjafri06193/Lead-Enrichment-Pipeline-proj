# Lead-Enrichment-Pipeline-proj

Event-driven enrichment service handling identity resolution, fuzzy dedupe, and bidirectional CRM sync with conflict reconciliation.

Or, in the revised form the design document arrives at after its reality check:

> An entity resolution and sync service built on an append-only observation
> store: probabilistic matching with blocking and measured precision, clustering
> with cohesion guards against transitive collapse, a review queue that precedes
> any auto-merge, echo-suppressed bidirectional sync governed by field-level
> source-of-truth policy, and an API budget manager that treats the customer's
> rate limit as a shared resource to be conserved.

The full design is in [`../Documentation/README.md`](../Documentation/README.md); code
comments refer to it by section number throughout.

---

## Quick start

```bash
cd v1
pip install -e ".[dev]"

lep policy          # print and validate the field policy (milestone M0)
lep demo            # run the whole pipeline against fake CRMs
pytest              # 137 tests, no services required
```

No database, no message broker and no API credentials are needed: the reference
store is SQLite and the CRMs are in-memory fakes that reproduce the behaviours
the design warns about — webhook echoes, lost attribution, dropped events,
schema drift and rate-limit headers.

## The four ideas this is built on

**Store observations, not values.** The current value of a field is never
stored; it is a pure function of an append-only, bitemporal observation log and
a field policy. Provenance is a query, a policy change is replayable against
history, a conflict exists by construction rather than being detected, and
erasure is provable. Everything else follows from this.

**A false merge and a false split are not the same kind of mistake.** A false
merge is irreversible data destruction and a privacy exposure; a false split is
a duplicate row. At 99% precision — a number most people would call excellent —
merging 100,000 pairs destroys a thousand records. So auto-merge ships *off*,
every pair goes to a review queue, and a score band is promoted to automatic
only when adjudicated decisions from real data say it clears the bar.

**Matching is not transitive.** A matches B, B matches C, A is clearly not C.
Connected components merges all three, and a chain of weak links collapses
hundreds of people into one record. Clusters are guarded on size, cohesion and
contradiction, and a failing cluster is split or reviewed, never merged.

**The API budget belongs to the customer, not to you.** HubSpot's daily limit is
shared across every app on the account; Salesforce pools REST, SOAP, Bulk and
Connect. Exhausting it breaks their whole integration ecosystem. Consumption is
read from provider response headers, 30% is reserved and never spent, and
backfill runs at a fraction of the remainder and pauses when real-time work
needs the headroom.

## Layout

```
src/lep/
  core/          types, similarity primitives
  store/         observations (append-only, bitemporal), entities, links,
                 merges, resolve()
  budget/        the shared-resource governor, provider limits, backfill
  identity/      normalization, deterministic rules, blocking, Fellegi-Sunter
                 scorer, Splink adapter, banded match policy
  cluster/       components, cohesion/size/contradiction guards
  review/        the queue that precedes auto-merge, precision by band
  sync/          echo suppression, circuit breaker, Salesforce/HubSpot
                 adapters, reconciliation, the sync engine
  conflict/      resolution strategies, field policy, conflict log
  enrich/        provider adapters, bake-off harness, TTLs
  events/        idempotency, ordering, DLQ, processor
  privacy/       erasure with evidence, suppression, provenance
  pipeline.py    the assembled system
  testing.py     simulated clock and the two-CRM soak harness
config/field-policy.yaml    ← M0: written and signed off before any sync code
docs/                       ← field policy rationale, matching, runbook, schema
tests/                      ← including the three tests from section 17.1
```

## The three tests that matter most

```bash
pytest tests/test_no_supercluster.py    # chains must not collapse
pytest tests/test_echo_loop.py          # 24 simulated hours, zero loops
pytest tests/test_erasure_complete.py   # every store, with evidence
```

The soak test runs against a simulated clock, which matters: echo suppression,
the write-log window and the circuit breaker are all time-relative, so a test
that fires a thousand edits at wall-clock speed measures the breaker rather than
the suppression.

## Where this deviates from the design document, and why

| Design | Here | Why |
|---|---|---|
| PostgreSQL with JSONB | SQLite, same schema shape | The whole system runs with no services. [`docs/schema.postgres.sql`](docs/schema.postgres.sql) is the production DDL; the access patterns are identical. |
| Splink on DuckDB | Splink adapter, plus a built-in Fellegi-Sunter with EM and TF adjustments | The document is right that you should not write your own — so `lep.identity.splink_model` uses Splink when installed (`pip install ".[splink]"`). The built-in scorer keeps the pipeline, tests and demo dependency-free, and has the same interface. |
| Temporal for workflows | `Backfill` with checkpoints and a priority-aware budget gate | The properties that matter (resumable, pausable, throttled) are in the code rather than in a service. Swap the driver, keep the semantics. |
| `src/store/`, `src/sync/`, … | `src/lep/store/`, `src/lep/sync/`, … | Top-level packages called `store`, `sync` and `events` collide with everything. One namespace package, same structure. |
| Double Metaphone | Compact implementation of the common rules | Covers the Slavic, Germanic, Spanish and Italian cases that matter for real contact data; `pip install metaphone` and swap the function for the full reference implementation in production. The tests pin the behaviour the pipeline relies on. |

## Status against the milestone ladder

| Milestone | State |
|---|---|
| M0 field policy | `config/field-policy.yaml`, validated at startup — a policy problem is a startup failure |
| M1 observation store | bitemporal, append-only, `resolve()` pure; policy changes replay |
| M2 budget manager | reserve, priority classes, header-read consumption, Bulk planning, resumable backfill |
| M3 ingestion | idempotency, out-of-order handling, DLQ with depth alerting; replay is order-independent |
| M4 deterministic matching | normalization, exact rules with per-rule confidence |
| M5 probabilistic matching | blocking with measured recall, EM and supervised training, TF adjustments |
| M6 review queue | uncertainty-ordered, decisions become labels, precision by band, promotion report |
| M7 clustering | size, cohesion and contradiction guards; balanced splitting; distribution monitoring |
| M8/M9 sync | per-field direction, both echo defenses, circuit breaker, polling reconciliation |
| M10 conflicts | six strategies, conflict log, top-conflicting-fields diagnostic, soft deletes |
| M11 enrichment | provider adapters, bake-off with cost per correct field, TTLs, `HUMAN_WINS` |
| M12 privacy | erasure across every store with a verified report, suppression, provenance |

What is deliberately *not* here: a reviewer UI (the queue and its API are, the
screen is not), real CRM credentials, and a Temporal deployment.
