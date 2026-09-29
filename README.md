# Lead Enrichment Pipeline

Event-driven enrichment service handling identity resolution, fuzzy dedupe, and
bidirectional CRM sync (HubSpot, Salesforce) with conflict reconciliation.

```
 CRM events ──► idempotent processor ──► append-only observation store
                                               │
                     identity resolution: blocking ► probabilistic scoring ► clustering (cohesion guards)
                                               │
                         review queue ◄── uncertain matches      enrichment (API budget manager)
                                               │
                     field-level source-of-truth policy ──► echo-suppressed bidirectional CRM sync
```

## Highlights

- **Append-only observation store** — every value keeps its source; nothing is overwritten.
- **Probabilistic matching** with blocking and measured precision, plus cohesion guards against "supercluster" collapse.
- **Review before auto-merge** for uncertain matches.
- **Echo suppression** so the pipeline's own writes don't bounce back as new events.
- **API budget manager** that treats the customer's CRM rate limit as a shared resource.
- **GDPR-style erasure** that's tested end to end.

## Quick start

No database, broker or credentials needed — it runs against fake CRMs:

```bash
cd v1
pip install -e ".[dev]"
lep policy      # print and validate the field policy
lep demo        # run the whole pipeline end to end
pytest          # 137 tests
```

## Repository layout

```
.
├── README.md          ← you are here
├── docs/
│   ├── design.md      ← full design guide (the spec code comments cite)
│   └── design.pdf     ← same guide, PDF
└── v1/                ← first implementation
    ├── src/lep/       identity, cluster, review, enrich, budget, events, store, conflict, sync, privacy
    ├── config/        field-policy.yaml (source of truth per field)
    ├── tests/         137 tests, incl. echo loop, erasure, no-supercluster
    └── docs/          matching, field policy, Postgres schema, runbooks, section→module map
```

Each `vN/` directory is a self-contained iteration. Start with
[`v1/README.md`](v1/README.md).

## Versions and feedback

| Version | Summary | Feedback |
|---|---|---|
| [v1](v1/) | Observation store, probabilistic identity resolution, review queue, budgeted enrichment, echo-safe CRM sync | — |

Add a row per version as new iterations land.
