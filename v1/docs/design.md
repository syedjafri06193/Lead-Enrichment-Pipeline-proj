# Design document

The design this repository implements lives at
[`../../Documentation/README.md`](../../Documentation/README.md) and is the canonical
copy.  It is referenced throughout the code by section number -- "section 5.4"
means "Lead Enrichment Pipeline — Design & Build Guide, section 5.4".

The map from its sections to the code:

| Section | Module |
|---|---|
| 3 The API budget you don't own | `lep.budget.manager`, `lep.budget.providers`, `lep.budget.backfill` |
| 4 The entity store | `lep.store.observations`, `lep.store.entities`, `lep.store.resolve` |
| 5.1 Deterministic first | `lep.identity.deterministic`, `lep.identity.normalize` |
| 5.2 Blocking | `lep.identity.blocking` |
| 5.3 Probabilistic scoring | `lep.identity.scorer`, `lep.identity.splink_model` |
| 5.4 The asymmetry and the precision bar | `lep.identity.policy`, `lep.review.queue` |
| 6 Clustering and the super-cluster trap | `lep.cluster.components`, `lep.cluster.guards` |
| 7 Bidirectional sync | `lep.sync.engine`, `lep.sync.echo`, `lep.sync.circuit`, `lep.sync.reconcile` |
| 8 Conflict reconciliation | `lep.conflict.strategies`, `lep.conflict.log` |
| 9 Event-driven mechanics | `lep.events.*` |
| 10 Enrichment data quality | `lep.enrich.*` |
| 11 Privacy and compliance | `lep.privacy.erasure` |
| 12 Schema drift and backfill | `lep.sync.salesforce`, `lep.budget.backfill` |
| 16 Reference implementations | `normalize_email`, `cohesion`, `LoopDetector` |
| 17 Testing | `tests/`, `lep.testing` |

Companion documents in this directory:

* [`field-policy.md`](field-policy.md) — why each field has the master and
  direction it has (milestone M0).
* [`matching.md`](matching.md) — thresholds, what is measured, and how a band
  gets promoted to auto-merge.
* [`runbook-superclusters.md`](runbook-superclusters.md) — what to do when a
  cluster collapses.
* [`schema.postgres.sql`](schema.postgres.sql) — the production DDL.
