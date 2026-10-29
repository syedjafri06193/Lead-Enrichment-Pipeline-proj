# Lead Enrichment Pipeline

[![CI](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj/actions/workflows/ci.yml/badge.svg)](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj/actions/workflows/ci.yml)

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
├── 1—Documentation/   ← all documentation (index in its README)
│   ├── design.md      ← full design guide (the spec code comments cite)
│   ├── design.pdf     ← same guide, PDF
│   └── v1/            matching, field policy, runbooks, section→module map
└── v1/                ← first implementation
    ├── src/lep/       identity, cluster, review, enrich, budget, events, store, conflict, sync, privacy
    ├── config/        field-policy.yaml (source of truth per field)
    ├── sql/           production Postgres DDL
    └── tests/         137 tests, incl. echo loop, erasure, no-supercluster
```

Each `vN/` directory is a self-contained iteration. Start with
[`v1/README.md`](v1/README.md).

## Versions and feedback

| Version | Summary | Feedback |
|---|---|---|
| [v1](v1/) | Observation store, probabilistic identity resolution, review queue, budgeted enrichment, echo-safe CRM sync | — |

Add a row per version as new iterations land.

## Documentation

All documentation lives in [`1—Documentation/`](1—Documentation/README.md) — start with its index.

<!-- project-metrics:start -->
## Project Metrics

[![Stars](https://img.shields.io/github/stars/syedjafri06193/Lead-Enrichment-Pipeline-proj?style=flat-square)](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj/stargazers) [![Forks](https://img.shields.io/github/forks/syedjafri06193/Lead-Enrichment-Pipeline-proj?style=flat-square)](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj/forks) [![Open issues](https://img.shields.io/github/issues/syedjafri06193/Lead-Enrichment-Pipeline-proj?style=flat-square)](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj/issues) [![Last commit](https://img.shields.io/github/last-commit/syedjafri06193/Lead-Enrichment-Pipeline-proj/improvements?style=flat-square)](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj/commits/improvements) [![Commits](https://img.shields.io/github/commit-activity/t/syedjafri06193/Lead-Enrichment-Pipeline-proj?style=flat-square)](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj/commits/improvements) [![Top language](https://img.shields.io/github/languages/top/syedjafri06193/Lead-Enrichment-Pipeline-proj?style=flat-square)](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj) [![Repo size](https://img.shields.io/github/repo-size/syedjafri06193/Lead-Enrichment-Pipeline-proj?style=flat-square)](https://github.com/syedjafri06193/Lead-Enrichment-Pipeline-proj)

### Code

| Metric | Value |
|---|---|
| Lines of code | 6,271 |
| Source files | 64 |
| Tracked files | 76 |
| Documentation | 8 Markdown files (12,359 words), 1 PDF |
| Commits | 10 |
| Contributors | 1 |
| Active | 2026-09-11 → 2026-10-28 |

| Language | Lines | Share |
|---|---:|---:|
| Python | 6,005 | 95.8% |
| Transact-SQL | 163 | 2.6% |
| YAML | 81 | 1.3% |
| TOML | 22 | 0.4% |

<sub>Code stats as of 2026-10-28, counted with pygount (comments and blank lines excluded). Badges update live.</sub>
<!-- project-metrics:end -->
