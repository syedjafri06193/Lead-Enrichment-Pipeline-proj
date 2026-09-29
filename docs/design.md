# Lead Enrichment Pipeline — Design & Build Guide

**Project:** Event-driven enrichment service handling identity resolution, fuzzy dedupe, and bidirectional CRM sync with conflict reconciliation
**Language:** Python
**Status of this document:** planning + reference

---

## Table of contents

1. [Executive summary and scope](#1-executive-summary-and-scope)
2. [Reality check](#2-reality-check)
3. [The API budget you don't own](#3-the-api-budget-you-dont-own)
4. [The entity store: observations, not values](#4-the-entity-store-observations-not-values)
5. [Identity resolution](#5-identity-resolution)
6. [Clustering and the super-cluster trap](#6-clustering-and-the-super-cluster-trap)
7. [Bidirectional sync](#7-bidirectional-sync)
8. [Conflict reconciliation](#8-conflict-reconciliation)
9. [Event-driven mechanics](#9-event-driven-mechanics)
10. [Enrichment data quality](#10-enrichment-data-quality)
11. [Privacy and compliance](#11-privacy-and-compliance)
12. [Schema drift and backfill](#12-schema-drift-and-backfill)
13. [Tech stack and setup](#13-tech-stack-and-setup)
14. [Repository layout](#14-repository-layout)
15. [Milestone ladder](#15-milestone-ladder)
16. [Reference implementations](#16-reference-implementations)
17. [Testing](#17-testing)
18. [Stretch goals](#18-stretch-goals)
19. [References](#19-references)

---

## 1. Executive summary and scope

### The original statement

> Event-driven enrichment service handling identity resolution, fuzzy dedupe, and bidirectional CRM sync with conflict reconciliation.

Five findings reshape this:

1. **Merge errors and split errors are not symmetric, and tuning for F1 treats them as if they were.** A false merge destroys data irreversibly — most CRMs cannot cleanly unmerge — and can expose one person's information inside another's record. A false split leaves a duplicate row. At 99% precision, merging 100,000 pairs produces **1,000 destroyed records**. Auto-merge needs a precision bar far higher than any published benchmark, with everything below it going to a review queue. See section 5.4.
2. **Bidirectional sync without echo suppression is an infinite loop, and it's the first thing you'll build wrong.** A writes to B, B's webhook fires, the change syncs back to A, A's webhook fires. Origin tagging alone is insufficient because CRMs don't reliably surface who made a change. See section 7.2.
3. **There is no correct generic conflict resolution algorithm, and last-write-wins is worse than it looks.** It requires synchronized clocks across systems you don't control, and "last" isn't "correct" — an automation that touched 10,000 records at 3am is last for all of them. The workable answer is field-level source-of-truth policy, which is configuration, not an algorithm. See section 8.
4. **You are spending an API budget you don't own, shared with every other integration your customer has.** Salesforce pools REST, SOAP, Bulk, and Connect calls in one rolling 24-hour allocation; HubSpot's daily limit is shared across every app on the account. Exhausting it breaks the customer's entire integration ecosystem, not just your feature. See section 3.
5. **Naive dedupe clustering creates super-clusters.** If A matches B and B matches C but A doesn't match C, connected-components clustering merges all three — and a chain of weak links can collapse hundreds of distinct people into one record. See section 6.2.

### Revised project statement

> An entity resolution and sync service built on an append-only observation store: probabilistic matching with blocking and measured precision, clustering with cohesion guards against transitive collapse, a review queue that precedes any auto-merge, echo-suppressed bidirectional sync governed by field-level source-of-truth policy, and an API budget manager that treats the customer's rate limit as a shared resource to be conserved.

### Explicit non-goals

- **Not a data provider.** You call enrichment APIs; you don't build the dataset.
- **Not a CRM.** You sync with them.
- **Not a master data management platform.** MDM for all enterprise entities is a decade-long program. You're doing people and companies.
- **Not real-time sub-second.** Enrichment and resolution in seconds to minutes is fine and far cheaper.
- **Not automatic merging of everything.** Some decisions go to humans permanently, and that's correct (§5.4).

---

## 2. Reality check

### 2.1 What makes this genuinely hard

Each of these has a well-known failure mode with a specific name:

| Problem | Failure mode |
|---|---|
| Identity resolution | False merges destroy data irreversibly |
| Fuzzy dedupe at scale | N² comparisons — 1M records is 500 billion pairs |
| Clustering | Transitive closure collapses distinct entities |
| Bidirectional sync | Echo loops |
| Conflict resolution | LWW loses data and depends on clocks you don't control |
| Event-driven | At-least-once delivery, out-of-order arrival |
| CRM integration | Shared API budget, schema drift |
| Enrichment | Providers disagree and decay |

None of these is exotic. All of them are things this project will hit in the first month.

### 2.2 The scale arithmetic

Comparing every record against every other is `n(n-1)/2`:

| Records | Pairs | At 1 µs/comparison |
|---|---|---|
| 10,000 | 50 million | 50 seconds |
| 100,000 | 5 billion | 83 minutes |
| 1,000,000 | **500 billion** | **5.8 days** |

**Blocking is not an optimization; it's the only way this runs at all.** Section 5.2.

### 2.3 What already exists

| Tool | What it is |
|---|---|
| **Splink** (UK MoJ) | Open-source probabilistic record linkage on DuckDB/Spark. Fellegi-Sunter with EM-trained weights. **Use it.** |
| dedupe.io / `dedupe` | Python active-learning record linkage |
| Zingg | Spark-based entity resolution |
| Senzing | Commercial, strong at entity resolution |
| Workato / Tray / Stacksync | Commercial bidirectional CRM sync |
| Reverse ETL (Census, Hightouch) | One-directional warehouse → SaaS; deliberately avoids bidirectional |

Worth noting that the reverse-ETL category **deliberately chose one-directional sync**. That's informative: a well-funded segment of this market looked at bidirectional and decided the conflict semantics weren't worth it. If you can get away with one direction per field, do.

**Don't write your own Fellegi-Sunter implementation.** Splink is mature, calibrated, and fast. The interesting engineering here is everything around it: blocking strategy, the precision bar, cluster validation, the review queue, and sync semantics.

---

## 3. The API budget you don't own ★

### 3.1 The numbers

**Salesforce** uses a rolling 24-hour pool:

```
Daily limit = Base + (per-license allocation × licenses) + add-ons
```

| Edition | Base | Per Salesforce license |
|---|---|---|
| Enterprise | 100,000 | +1,000/user |
| Unlimited / Performance | 100,000 | +5,000/user |
| Developer | 15,000 | — (hard cap) |

Other Salesforce limits that bite:

| Limit | Value |
|---|---|
| Bulk API 2.0 — data per job | 150 MB encoded (keep under 100 MB practical) |
| Bulk API — records per internal batch | 10,000 (auto-chunked) |
| Bulk API — daily batch submissions | 15,000 per rolling 24h (shared 1.0/2.0) |
| Bulk API — concurrent jobs | 25 |
| Concurrent long-running requests (20s+) | 25 production / 5 developer |
| API call timeout | 10 minutes, hard |

**One thing to verify against your own org:** sources disagree on whether Bulk API 2.0 consumes the daily REST allocation or has a separate governor. Don't trust any blog on this, including this one — check **Setup → Company Information → API Requests, Last 24 Hours** on the actual org while running a Bulk job and watch what moves.

**HubSpot:**

| Plan | Burst (per 10s) | Daily |
|---|---|---|
| Free / Starter private app | 100 | 250,000 |
| Professional private app | 190 | 625,000 |
| Enterprise private app | 190 | 1,000,000 |
| OAuth marketplace app | 110 per installed account | per app category |
| **CRM Search API** | **5 per second** | shares the daily cap |

That Search API limit is much tighter than the general one and is easy to blow through during matching, which is exactly when you want to search.

### 3.2 The budget is shared ★

This is the architectural point. **HubSpot's daily limit is shared across every app on the account.** Salesforce pools REST, SOAP, Bulk, and Connect in one allocation.

So when your enrichment backfill consumes 400,000 calls, you have taken that from the customer's marketing automation, their billing sync, their data warehouse ETL, and their internal scripts. Salesforce returns HTTP 403 on sustained excess, and **everything** stops — not just you.

Being the integration that broke a customer's entire Salesforce is a specific and memorable way to lose an account.

### 3.3 Treat it as a managed resource

```python
@dataclass
class ApiBudget:
    org_id: str
    provider: Literal["salesforce", "hubspot"]
    daily_limit: int
    reserve_fraction: float = 0.30   # never consume the customer's last 30%
    consumed: int = 0
    window_start: datetime = ...

    @property
    def available(self) -> int:
        usable = int(self.daily_limit * (1 - self.reserve_fraction))
        return max(0, usable - self.consumed)
```

Rules worth enforcing:

- **Reserve headroom.** Never consume more than ~70% of the allocation. The remainder belongs to the customer's other integrations and to unexpected load.
- **Read consumption from the provider**, not from your own counter. Salesforce exposes usage in the `Sforce-Limit-Info` response header; HubSpot returns `X-HubSpot-RateLimit-*`. Your own count drifts and misses everything else on the account.
- **Prioritize.** When budget is tight, real-time user-triggered operations run and background backfill pauses. A queue with priority classes, not FIFO.
- **Bulk API for anything over ~2,000 records.** One Bulk job versus 2,000 REST calls is the difference between a viable backfill and an incident.
- **Batch reads.** A `WHERE Id IN (...)` query for 200 records is one call; 200 individual gets is 200.
- **Cache aggressively.** Field metadata, picklist values, user lists, and record types change rarely and get fetched constantly.
- **Surface consumption to the customer.** "This sync used 12% of your Salesforce API allocation today" builds trust and prevents surprises.

### 3.4 Backfill is the dominant consumer

An initial sync of 500,000 records is an entirely different workload from ongoing deltas, and the arithmetic should be done before the first customer.

| Approach | Calls for 500k records |
|---|---|
| REST, one record at a time | 500,000 — **impossible** |
| REST, batched at 200 | 2,500 |
| Bulk API 2.0 | ~50 jobs |

Backfill should be throttled to a configured fraction of daily budget, resumable from a checkpoint, and able to run for days without anyone worrying. A backfill that must complete in one window is a backfill that will exhaust the customer's limit.

---

## 4. The entity store: observations, not values ★

### 4.1 Store what was observed, resolve on read

The natural design is a table of entities with current field values. It's also the design that makes conflict resolution impossible to debug and impossible to change your mind about.

Instead, store **observations**:

```sql
CREATE TABLE observations (
    id             BIGSERIAL PRIMARY KEY,
    entity_id      UUID NOT NULL,
    field          TEXT NOT NULL,
    value          JSONB NOT NULL,
    source         TEXT NOT NULL,      -- salesforce | hubspot | clearbit | user | ...
    source_record_id TEXT,
    confidence     REAL,
    observed_at    TIMESTAMPTZ NOT NULL,   -- source system's timestamp
    recorded_at    TIMESTAMPTZ NOT NULL,   -- when WE learned it
    expires_at     TIMESTAMPTZ,            -- staleness TTL (§10.3)
    superseded_by  BIGINT REFERENCES observations(id)
);

CREATE INDEX ON observations (entity_id, field, observed_at DESC);
```

The current value of a field is then a **pure function** of the observations and the resolution policy:

```python
def resolve(entity_id: str, field: str, policy: FieldPolicy,
            at: datetime | None = None) -> ResolvedValue:
    """Current value = f(observations, policy). Nothing is overwritten,
    so policy changes can be replayed against history."""
    obs = store.observations(entity_id, field, as_of=at)
    obs = [o for o in obs if not o.is_expired(at)]
    return policy.resolve(obs)
```

Four things this buys you:

- **Provenance is free.** "Where did this employee count come from?" is a query, not an investigation.
- **Policy changes are replayable.** Change the source-of-truth for a field and re-resolve history. With overwrites, the old values are gone.
- **Conflicts are visible.** Two observations with different values are a conflict by construction, whether or not you auto-resolved it.
- **Erasure is tractable.** GDPR deletion means deleting observation rows, and you can prove what was removed (§11.2).

Two timestamps matter separately. `observed_at` is the source system's notion of when the value was true; `recorded_at` is when you learned it. They differ when a webhook is delayed or a backfill imports history, and conflating them makes ordering wrong (§9.2). This is a bitemporal model and the complexity is worth it here.

### 4.2 Entities and links

```sql
CREATE TABLE entities (
    id         UUID PRIMARY KEY,
    type       TEXT NOT NULL,        -- person | company
    created_at TIMESTAMPTZ NOT NULL
);

-- Which external records are believed to be this entity
CREATE TABLE entity_links (
    entity_id     UUID NOT NULL REFERENCES entities(id),
    source        TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    confidence    REAL NOT NULL,
    linked_at     TIMESTAMPTZ NOT NULL,
    linked_by     TEXT NOT NULL,     -- 'auto:v3' | 'user:alice@...'
    PRIMARY KEY (source, source_record_id)
);

-- Merges are recorded, never destructive
CREATE TABLE merges (
    id           BIGSERIAL PRIMARY KEY,
    surviving_id UUID NOT NULL,
    merged_id    UUID NOT NULL,
    score        REAL,
    decided_by   TEXT NOT NULL,
    decided_at   TIMESTAMPTZ NOT NULL,
    reverted_at  TIMESTAMPTZ,
    revert_reason TEXT
);
```

**Merges are recorded as links, not as deletions.** The merged entity's ID stays resolvable and its observations stay attached. That makes unmerge possible in your system even though it isn't in the CRM — which matters enormously given §5.4.

---

## 5. Identity resolution

### 5.1 Deterministic first

Cheap, high-precision rules run before any probabilistic work:

| Rule | Precision |
|---|---|
| Normalized email exact match | Very high |
| Same external ID from the same source | Certain |
| Verified phone + last name | High |
| LinkedIn profile URL | High |

Normalize before comparing: lowercase, strip Gmail dots and `+` tags, trim whitespace. But be careful — Gmail ignores dots, most other providers do not, so the normalization must be provider-aware or you'll merge `j.smith@company.com` and `jsmith@company.com` at a non-Gmail domain where they may be different people.

Deterministic rules will resolve most of your volume. Probabilistic matching handles the remainder.

### 5.2 Blocking

You cannot compare all pairs (§2.2). Blocking generates candidate pairs that share some key, and only those get scored.

```python
BLOCKING_RULES = [
    "l.email_domain = r.email_domain AND l.last_name_dm = r.last_name_dm",
    "l.normalized_company = r.normalized_company AND l.first_initial = r.first_initial",
    "l.phone_e164 = r.phone_e164",
    "l.last_name_dm = r.last_name_dm AND l.first_name_dm = r.first_name_dm",
]
```

Design notes:

- **Multiple rules, unioned.** Any single rule misses pairs; several overlapping rules recover most of them.
- **Measure recall loss.** Blocking that misses true pairs caps your recall no matter how good the scorer is. Take a labeled set and check what fraction of known matches survive blocking.
- **Watch for blocks that explode.** Blocking on `email_domain` alone puts every `gmail.com` address in one block. Always conjoin with something selective.
- Use Double Metaphone (`_dm`) rather than Soundex — it handles non-English names considerably better, which matters for real contact data.

### 5.3 Probabilistic scoring with Splink

Fellegi-Sunter compares field agreement patterns and computes a match weight from two probabilities per comparison level: `m` (agreement given a true match) and `u` (agreement given a non-match). Splink estimates these with EM and produces calibrated match probabilities.

```python
import splink.comparison_library as cl
from splink import Linker, DuckDBAPI, SettingsCreator

settings = SettingsCreator(
    link_type="dedupe_only",
    blocking_rules_to_generate_predictions=BLOCKING_RULES,
    comparisons=[
        cl.EmailComparison("email"),
        cl.NameComparison("first_name"),
        cl.NameComparison("last_name"),
        cl.JaroWinklerAtThresholds("company_name", [0.9, 0.8]),
        cl.ExactMatch("phone_e164").configure(term_frequency_adjustments=True),
    ],
    retain_intermediate_calculation_columns=True,
)
```

**Term frequency adjustments matter more than people expect.** Two records agreeing on the surname "Nguyen" is weak evidence; agreeing on "Featherstonehaugh" is strong. Without TF adjustment both count the same, and common-name false merges are a leading cause of the super-cluster problem in §6.2.

### 5.4 The asymmetry, and the precision bar ★

**A false merge and a false split are not comparable errors.**

| | False merge | False split |
|---|---|---|
| Effect | Two people become one record | One person has two records |
| Reversibility | **Effectively irreversible in most CRMs** | Trivially fixable |
| Data loss | One record's field values lost | None |
| Privacy | Person A's data now sits in person B's record | None |
| Downstream | Wrong person emailed, wrong data in reports | Slightly inflated counts |
| Detection | Often never noticed | Obvious |

Now the arithmetic. Suppose a model at **99% precision** on merge decisions, running against 100,000 candidate merges:

**1,000 wrong merges. One thousand pairs of distinct people silently collapsed into single records, unrecoverably.**

99% precision is a number most practitioners would call excellent. For this decision it is unacceptable.

So the threshold structure is banded, not binary:

```python
@dataclass
class MatchPolicy:
    auto_merge_threshold: float = 0.995    # calibrated, and MEASURED (§17.2)
    review_threshold: float = 0.80
    # below review_threshold → distinct, no action

def decide(score: float, policy: MatchPolicy) -> Decision:
    if score >= policy.auto_merge_threshold:
        return Decision.AUTO_MERGE
    if score >= policy.review_threshold:
        return Decision.REVIEW          # a human looks at it
    return Decision.DISTINCT
```

Three consequences worth stating plainly:

- **Build the review queue before auto-merge.** Route everything through review first, measure precision in each score band on real decisions, and only then promote a band to auto. The precision number must come from your data, not from a benchmark.
- **Tune for precision at a fixed recall, not F1.** F1 treats the two error types as equal, which is the mistake this whole section exists to prevent.
- **Auto-merge is reversible in your store, even though it isn't in the CRM.** §4.2's merge records and retained observations are what let you undo a mistake once you find it.

### 5.5 Companies are harder than people

People have email addresses. Companies have:

- Legal name vs. trading name ("Alphabet Inc." vs "Google")
- Suffix variation (Inc, Inc., Incorporated, LLC, Ltd, GmbH, K.K.)
- Subsidiaries and divisions — same or different?
- Acquisitions changing the answer over time
- Domain as the strongest signal, but multi-domain companies and shared hosting break it

Normalize suffixes, use domain when available, and accept that "is this the same company" is sometimes genuinely ambiguous — a division of a conglomerate may be the same company for billing and a different one for sales territory. Let the policy declare which definition you're using.

---

## 6. Clustering and the super-cluster trap

### 6.1 From pairs to entities

Pairwise scores give you edges. Entities are the clusters. The naive step is connected components: any chain of edges above threshold becomes one cluster.

### 6.2 Why connected components fails ★

Matching is **not transitive**. A can match B, B can match C, and A can be clearly distinct from C.

```
"J. Smith, Acme"  ←→  "John Smith, Acme"  ←→  "John Smith, Acme Corp"  ←→  "Jon Smith, Acme"
     0.85                      0.91                       0.87
```

Connected components merges all four. Add a few hundred more records and a chain of weak links collapses a large population of genuinely distinct people into one entity. The characteristic symptom is a single record with forty email addresses and a job title from an industry nobody in the cluster works in.

It's worse with common names — which is exactly why term frequency adjustments (§5.3) matter.

### 6.3 Guards

```python
def validate_cluster(cluster: set[str], edges: EdgeIndex, cfg: ClusterConfig) -> ClusterVerdict:
    n = len(cluster)
    if n <= 1:
        return ClusterVerdict.OK

    if n > cfg.max_size:
        return ClusterVerdict.TOO_LARGE          # → review, never auto-merge

    # Cohesion: what fraction of possible internal pairs actually scored
    # above threshold? A chain has cohesion ~2/n; a real cluster is near 1.
    possible = n * (n - 1) / 2
    actual = edges.count_within(cluster, above=cfg.edge_threshold)
    if actual / possible < cfg.min_cohesion:
        return ClusterVerdict.LOW_COHESION       # → split or review

    # No pair inside a cluster may be strongly evidenced as distinct.
    if edges.any_within(cluster, below=cfg.contradiction_threshold):
        return ClusterVerdict.CONTRADICTED
    return ClusterVerdict.OK
```

**Cohesion is the key metric.** A four-record chain has three edges out of six possible pairs — cohesion 0.5. A genuine cluster of four duplicates has close to six. Requiring cohesion above ~0.7 kills chains while preserving real clusters.

Practical settings:

| Guard | Starting value |
|---|---|
| `max_size` | 10 for people, 25 for companies |
| `min_cohesion` | 0.7 |
| `contradiction_threshold` | 0.2 |

Clusters failing any guard go to review, never to auto-merge. Then **look at the distribution of cluster sizes after every model change**. A sudden shift toward large clusters means the threshold moved or the data changed, and it's the earliest warning you'll get.

---

## 7. Bidirectional sync

### 7.1 Decide direction per field, not per object

Full bidirectional sync of every field is unbounded scope with unbounded conflict surface. Most fields have an obvious owner.

```yaml
objects:
  contact:
    fields:
      email:
        master: salesforce
        direction: sf_to_hubspot
      first_name:
        master: salesforce
        direction: bidirectional        # genuinely edited in both
      lifecycle_stage:
        master: hubspot
        direction: hubspot_to_sf
      owner_id:
        master: salesforce
        direction: sf_only              # never written from outside
      employee_count:
        master: enrichment
        direction: enrichment_to_all
        ttl_days: 90
      notes:
        direction: none                 # deliberately not synced
```

**Most fields end up one-directional.** That's the right outcome — every bidirectional field is a conflict you've signed up to resolve. The reverse-ETL category built entire successful products on one-directional sync (§2.3); don't reach for bidirectional by default.

### 7.2 Echo suppression ★

The loop:

```
1. user edits Salesforce                → SF webhook fires
2. sync writes to HubSpot               → HubSpot webhook fires
3. sync writes back to Salesforce       → SF webhook fires
4. → 2  (forever)
```

Two defenses, and you need both.

**Origin tagging** — check whether the change was made by your own integration user:

```python
def is_own_write_by_actor(event: CrmEvent, integration_user_id: str) -> bool:
    if event.source == "salesforce":
        return event.last_modified_by_id == integration_user_id
    if event.source == "hubspot":
        # Property history carries sourceId / sourceType
        return event.property_source_id == integration_user_id
    return False
```

This works when the CRM surfaces the modifier — and it doesn't always. Some webhook payloads omit it; some workflow-triggered changes attribute to a different user; formula and roll-up recalculations attribute to nobody.

**A write log** — more robust, because it doesn't depend on the CRM preserving anything:

```python
@dataclass
class WriteRecord:
    target: str          # "salesforce"
    record_id: str
    field: str
    value_hash: str
    written_at: datetime

def is_echo(event: CrmEvent, log: WriteLog, window: timedelta = timedelta(minutes=5)) -> bool:
    """We wrote this exact value to this exact field recently, so this
    inbound event is our own write coming back."""
    recent = log.lookup(event.source, event.record_id, event.field, window)
    return any(w.value_hash == hash_value(event.new_value) for w in recent)
```

Hash the value rather than storing it — the log gets large, and you only need equality.

**Use both.** Origin tagging catches echoes fast and cheaply; the write log catches the ones where attribution was lost. And add a circuit breaker: if a single record changes more than N times in M minutes, stop syncing it and alert. That catches loops your suppression missed, which is the failure you most want to fail loudly.

### 7.3 Change detection

| Mechanism | Salesforce | HubSpot |
|---|---|---|
| Push | Change Data Capture, Platform Events, Streaming API | Webhooks |
| Pull | `SystemModstamp` polling | `hs_lastmodifieddate` search |

Push is lower latency and cheaper in API calls. **Run polling reconciliation anyway** — every push mechanism drops events, and a drifted record that nobody notices for a month is worse than a slightly stale one.

Poll on `SystemModstamp`, not `LastModifiedDate`: `SystemModstamp` also advances on system-level changes that `LastModifiedDate` misses.

Log the reconciliation discrepancy rate. A rising rate means your push path is broken.

---

## 8. Conflict reconciliation

### 8.1 Why last-write-wins is wrong

It's the obvious choice and it fails in four distinct ways:

**Clock skew across systems you don't control.** Salesforce timestamps come from Salesforce's clock, HubSpot's from HubSpot's, yours from yours. Seconds of skew is enough to reverse the ordering of near-simultaneous edits, and there's no way to correct for it.

**"Last" is not "correct."** A nightly automation that touches 10,000 records is last for all of them, beating a human who carefully corrected one an hour earlier.

**Record-level LWW loses unrelated edits.** Someone fixes a phone number in Salesforce; someone fixes a job title in HubSpot. Record-level resolution discards one entirely, including the field nobody touched.

**It's invisible.** Data silently disappears and nobody knows a conflict occurred.

### 8.2 Field-level source-of-truth

The workable answer is policy, declared per field:

```python
class ResolutionStrategy(Enum):
    SOURCE_PRIORITY = "source_priority"   # ranked sources; highest wins
    MOST_RECENT     = "most_recent"       # LWW, within ONE source only
    MOST_CONFIDENT  = "most_confident"    # highest-confidence observation
    HUMAN_WINS      = "human_wins"        # any user edit beats any automated one
    NEVER_OVERWRITE = "never_overwrite"   # first non-null value sticks
    MANUAL          = "manual"            # always escalate
```

```python
def resolve(obs: list[Observation], policy: FieldPolicy) -> ResolvedValue:
    live = [o for o in obs if not o.is_expired()]
    if not live:
        return ResolvedValue(None, reason="no live observations")

    if policy.strategy is ResolutionStrategy.HUMAN_WINS:
        human = [o for o in live if o.source_kind == "user"]
        if human:
            return ResolvedValue(max(human, key=lambda o: o.observed_at).value,
                                 reason="human edit")

    if policy.strategy is ResolutionStrategy.SOURCE_PRIORITY:
        for src in policy.source_ranking:
            match = [o for o in live if o.source == src]
            if match:
                # LWW is acceptable WITHIN one source — same clock.
                return ResolvedValue(max(match, key=lambda o: o.observed_at).value,
                                     reason=f"source priority: {src}")
    ...
```

Note the comment on LWW. **Within a single source, most-recent-wins is fine** because the timestamps come from one clock. Across sources it isn't. That distinction is what makes `SOURCE_PRIORITY` with a within-source recency tiebreak the right default.

`HUMAN_WINS` deserves to be the default for most user-editable fields. A person who corrected a record did so for a reason, and having an enrichment provider overwrite it an hour later is the single fastest way to lose user trust in the system.

### 8.3 Detect conflicts even when you resolve them

```python
@dataclass
class Conflict:
    entity_id: str
    field: str
    candidates: list[Observation]
    resolved_to: Any
    resolution_reason: str
    detected_at: datetime
    reviewed: bool = False
```

**Log every conflict, including auto-resolved ones.** The conflict log tells you whether your policy is right. A field generating constant conflicts usually means the source-of-truth assignment is wrong, or two teams are editing the same thing in two systems — an organizational problem your data can surface.

Surface the top conflicting fields in the admin UI. It's the most useful diagnostic the system produces, and it's free once observations are stored properly (§4.1).

### 8.4 Deletes are conflicts too

A record deleted in Salesforce and still present in HubSpot is a conflict, and it's the one where getting it wrong is most costly.

**Default to soft-delete and never propagate hard deletes automatically.** A propagated delete that shouldn't have happened destroys data across multiple systems at once. Route deletes to review, or mark as inactive and let a human decide. The asymmetry is the same as §5.4: an unwanted delete is unrecoverable, an un-propagated one is untidy.

---

## 9. Event-driven mechanics

### 9.1 At-least-once means idempotent everything

Every webhook provider redelivers. Every queue redelivers. Design for it rather than hoping.

```python
def handle(event: CrmEvent) -> None:
    key = f"{event.source}:{event.record_id}:{event.change_id}"
    if not store.claim_event(key, ttl=timedelta(days=7)):
        log.debug("duplicate event, skipping", key=key)
        return
    process(event)
```

Where the provider gives a stable change identifier, use it. Where it doesn't, hash the meaningful payload — but be careful to exclude fields that vary between redeliveries (delivery timestamps, attempt counters) or every redelivery looks new.

### 9.2 Out-of-order arrival

Webhooks arrive out of order routinely. An update can precede the create it depends on.

```python
def apply(obs: Observation) -> None:
    latest = store.latest_observation(obs.entity_id, obs.field, source=obs.source)
    if latest and obs.observed_at <= latest.observed_at:
        store.record_superseded(obs)     # keep it, don't apply it
        return
    store.apply(obs)
```

Compare on `observed_at` **within a source** (§8.2 — one clock). Across sources, ordering is resolved by policy, not by timestamp.

**Store out-of-order observations rather than discarding them.** They're evidence, they're needed for replay after a policy change, and a late-arriving observation may become the winner under a different policy.

For a create-after-update, create a provisional entity and let the create fill it in. Discarding orphan updates loses data that will not be redelivered.

### 9.3 Poison messages

An event that always fails will retry forever and block everything behind it.

```python
MAX_ATTEMPTS = 5

def process_with_dlq(event: CrmEvent) -> None:
    try:
        handle(event)
    except Exception as exc:
        attempts = store.increment_attempts(event.id)
        if attempts >= MAX_ATTEMPTS:
            dlq.send(event, error=str(exc))
            alert("event moved to DLQ", event_id=event.id, error=str(exc))
            return
        queue.retry(event, delay=backoff(attempts))
```

**Someone must look at the DLQ.** A dead-letter queue nobody monitors is a data-loss mechanism with extra steps. Alert on depth, not just on individual failures.

---

## 10. Enrichment data quality

### 10.1 Coverage claims mean less than they sound

A provider advertising "95% coverage" almost always means 95% of submitted records came back with *something*, not that 95% of returned values are correct. Those are very different numbers and only one of them matters.

### 10.2 Measure your providers against each other

Providers disagree — on employee count, industry, revenue, and job title — for the same company, routinely and substantially.

Build a bake-off before committing:

```python
def bake_off(records: list[Record], providers: list[Provider],
             gold: dict[str, dict]) -> BakeOffReport:
    """Send the same 500-1000 records to every provider, compare against a
    hand-verified gold set. Report per-field coverage AND accuracy, plus
    cost per correctly-enriched field."""
```

Report **per field**, not overall. A provider may be excellent on firmographics and poor on contact data, which means the right answer is often two providers with a field-level preference order — which is exactly what §8.2's `SOURCE_PRIORITY` expresses.

**Cost per correctly-enriched field** is the metric that actually decides. A cheaper provider with half the accuracy is more expensive.

### 10.3 Staleness

B2B contact data decays substantially year over year, mostly from job changes. Enrichment has a shelf life.

```python
FIELD_TTL = {
    "job_title":      timedelta(days=90),
    "company":        timedelta(days=90),
    "email":          timedelta(days=180),
    "employee_count": timedelta(days=180),
    "industry":       timedelta(days=365),
    "founded_year":   None,              # immutable
}
```

Expired observations are excluded from resolution (§4.1) but not deleted — they're still history, and they're evidence that a value changed.

**Re-enrichment is a budget decision, not a schedule.** Re-enriching everything quarterly is expensive; re-enriching records with active engagement is targeted. Prioritize by whether anyone is actually using the record.

### 10.4 Never let enrichment overwrite a human

The single fastest way to destroy trust: a rep corrects a job title, and an enrichment run overwrites it the next morning with the stale value.

This is what `HUMAN_WINS` in §8.2 is for, and it should be the default for every human-editable field. Enrichment fills gaps; it doesn't correct people.

---

## 11. Privacy and compliance

> Not legal advice. These are the questions to bring to counsel.

### 11.1 Enrichment is processing personal data

Under GDPR, enriching a person's record from third-party sources is processing, and it needs a lawful basis. **Article 14 additionally requires informing the data subject when data is obtained from sources other than them**, subject to exceptions. Most B2B enrichment operates on a legitimate-interest theory, which is arguable but contested, and several enrichment providers have drawn regulatory attention in the EU.

### 11.2 Erasure must propagate everywhere

A deletion request has to reach:

- Observations for that entity
- Entity links and merge records
- Cached enrichment payloads
- Search and matching indexes
- Derived clusters
- Event logs and DLQ contents
- Backups (or a documented policy on backup expiry)
- The CRMs you sync to

```python
def erase(entity_id: str, reason: str) -> ErasureReport:
    """Must cover every store. The report is the evidence that it did."""
    report = ErasureReport(entity_id=entity_id, reason=reason)
    report.observations   = store.delete_observations(entity_id)
    report.links          = store.delete_links(entity_id)
    report.search_index   = search.delete(entity_id)
    report.cluster_member = clusters.remove(entity_id)
    report.suppression    = suppression.add(entity_id, reason="erased")
    report.crm_requests   = [crm.request_deletion(l) for l in links]
    return report
```

Note the suppression entry. **Add an erasure suppression record so re-enrichment or a re-import doesn't resurrect the person.** Without it, the next backfill undoes the deletion — a failure mode that is both common and exactly what the regulation is about.

Test erasure with a test that searches every store afterward and asserts nothing remains.

### 11.3 Provenance is a compliance feature

"Where did this data come from?" is a question a regulator can ask, and §4.1's observation model answers it directly. The bitemporal store isn't just good engineering; it's the artifact that makes a data subject access request answerable.

---

## 12. Schema drift and backfill

### 12.1 Schemas change underneath you

CRM admins add fields, rename them, change picklist values, and deactivate record types — without telling you.

- **Read field metadata at startup and periodically**, don't hardcode it
- **Validate the mapping** against live metadata and fail loudly on a missing field rather than silently skipping it
- **Version the mapping** and record which version produced each observation
- **Handle picklist drift**: writing an invalid picklist value errors; reading a value you don't recognize should not crash
- **Watch for renames**, which look like a delete plus an add and will orphan your mapping

### 12.2 Backfill is its own system

| | Backfill | Incremental |
|---|---|---|
| Volume | Everything | Deltas |
| API strategy | Bulk API, throttled | REST, batched |
| Duration | Hours to days | Continuous |
| Failure handling | Resume from checkpoint | Retry the event |
| Priority | Lowest | Normal to high |

Backfill must checkpoint, resume, run at a configured fraction of API budget, and be pausable when real-time work needs the headroom. A backfill that can't be paused is a backfill that will take down the customer's integrations during their quarter end.

---

## 13. Tech stack and setup

| Layer | Choice | Why |
|---|---|---|
| **Language** | Python | Splink, the data ecosystem, and fast iteration on matching |
| **Matching** | **Splink** on DuckDB | Mature Fellegi-Sunter with EM training and calibrated probabilities. Don't write your own. |
| **Store** | PostgreSQL | JSONB observations, `pg_trgm` for fuzzy blocking, real transactions |
| **Analytics/batch** | DuckDB | Splink's backend; excellent over Parquet |
| **Workflow** | Temporal | Long-running backfills, retries, cancellation, visibility |
| **Queue** | SQS / Kafka, or Temporal task queues | |
| **CRM clients** | `simple-salesforce`, HubSpot SDK | |
| **Dataframes** | Polars | |

Two notes:

**`pg_trgm` is underrated for blocking.** Trigram similarity indexes in Postgres let you generate candidate pairs in-database without moving data, which is often enough at mid scale and much simpler than a separate pipeline.

**Get a Salesforce Developer Edition org and a HubSpot developer account on day one.** Both are free. The Developer org's 15,000-call hard cap is actually a useful forcing function — if your design works there, it will work anywhere.

---

## 14. Repository layout

```
Lead-Enrichment-Pipeline/
├── README.md
├── docs/
│   ├── design.md                ← this document
│   ├── field-policy.md          ← ★ per-field master and direction
│   ├── matching.md              ← thresholds, measured precision
│   └── runbook-superclusters.md
├── src/
│   ├── store/
│   │   ├── observations.py      ← ★ bitemporal, append-only
│   │   ├── entities.py
│   │   └── resolve.py           ← pure function: obs + policy → value
│   ├── budget/
│   │   ├── manager.py           ← ★ the shared-resource governor
│   │   └── providers.py         ← reads limits from response headers
│   ├── identity/
│   │   ├── normalize.py
│   │   ├── deterministic.py
│   │   ├── blocking.py
│   │   ├── splink_model.py
│   │   └── policy.py            ← the banded thresholds
│   ├── cluster/
│   │   ├── components.py
│   │   └── guards.py            ← ★ cohesion, size, contradiction
│   ├── review/                  ← the queue that precedes auto-merge
│   ├── sync/
│   │   ├── echo.py              ← ★ origin tagging + write log
│   │   ├── salesforce.py
│   │   ├── hubspot.py
│   │   ├── reconcile.py         ← polling backstop
│   │   └── circuit.py           ← loop detection
│   ├── conflict/
│   │   ├── strategies.py
│   │   └── log.py
│   ├── enrich/
│   │   ├── providers/
│   │   ├── bakeoff.py
│   │   └── ttl.py
│   ├── events/
│   │   ├── idempotency.py
│   │   ├── ordering.py
│   │   └── dlq.py
│   └── privacy/
│       └── erasure.py
└── tests/
    ├── test_no_supercluster.py  ← ★
    ├── test_echo_loop.py        ← ★
    ├── test_erasure_complete.py
    └── fixtures/
```

---

## 15. Milestone ladder

### M0 — Field policy ★ **before any sync code**
**Est. 4–5 days**

Write `docs/field-policy.md`: which objects, which fields, master per field, direction per field, resolution strategy per field, TTLs. Get the data owner to sign off.

Most fields should end up one-directional. Every bidirectional field is a conflict you're volunteering for.

**Done when:** every field in scope has a declared master, direction, and strategy.

---

### M1 — The observation store ★
**Est. 1.5 weeks**

Bitemporal observations, entities, links, merge records, and `resolve()` as a pure function.

**Done when:** changing a field policy and re-resolving produces different current values from the same history, with nothing lost.

---

### M2 — API budget manager ★ **before any bulk operation**
**Est. 1 week**

Consumption read from provider response headers, reserve fraction, priority queue, Bulk API path, caching.

**Build this before the first backfill.** It exists to stop you breaking a customer's Salesforce, and the first backfill is when that happens.

**Done when:** a simulated backfill against a Developer org's 15,000-call cap completes over multiple days without exhausting the allocation.

---

### M3 — Ingestion
**Est. 1.5 weeks**

Webhooks, polling reconciliation, idempotency, out-of-order handling, DLQ with alerting.

**Done when:** replaying the same event stream ten times in random order produces identical resolved state.

---

### M4 — Deterministic matching
**Est. 1 week**

Normalization, exact-match rules, entity linking. This resolves most volume.

---

### M5 — Probabilistic matching with measurement ★
**Est. 2 weeks**

Blocking with measured recall, Splink model with EM training and TF adjustments, calibration.

**Build a labeled set first** — a few thousand hand-adjudicated pairs. Without it you have no precision number and no basis for any threshold.

**Done when:** you can state measured precision at each score band, from your data.

---

### M6 — The review queue ★ **before auto-merge**
**Est. 1.5 weeks**

Queue, side-by-side comparison UI, merge/split/defer, and decisions captured as labels.

**Everything routes through review initially.** Reviewer decisions are labeled training data and the source of the precision measurement that eventually justifies auto-merge.

---

### M7 — Clustering with guards
**Est. 1 week**

Connected components plus size, cohesion, and contradiction guards; cluster-size distribution monitoring.

**Done when:** a deliberately constructed chain of weak matches does not produce a single merged cluster.

---

### M8 — One-directional sync
**Est. 1.5 weeks**

Write to one CRM from resolved values, with mapping validation and schema-drift handling.

---

### M9 — Bidirectional sync ★
**Est. 2 weeks**

Echo suppression via both origin tagging and write log, loop circuit breaker, reconciliation.

**Done when:** a soak test running bidirectional sync for 24 hours with continuous edits on both sides produces zero echo loops and zero unexplained writes.

---

### M10 — Conflict reconciliation
**Est. 1 week**

Strategies, conflict log, admin surfacing of top conflicting fields, soft-delete handling.

---

### M11 — Enrichment
**Est. 1.5 weeks**

Provider adapters, bake-off harness, TTLs, `HUMAN_WINS` enforcement.

---

### M12 — Privacy
**Est. 1 week**

Erasure across every store with a report, erasure suppression, provenance queries for DSARs.

**Done when:** an erasure test searches every store and finds nothing.

---

## 16. Reference implementations

### 16.1 Email normalization, provider-aware

```python
GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}

def normalize_email(email: str) -> str:
    """Gmail ignores dots and +tags. Most other providers do NOT, so
    normalizing globally would merge distinct people at other domains."""
    email = email.strip().lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)

    if domain in GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    else:
        # +tag stripping is broadly safe; dot stripping is NOT.
        local = local.split("+", 1)[0]

    return f"{local}@{domain}"
```

The asymmetry between dot-stripping and tag-stripping is the kind of detail that produces a class of false merges nobody traces back for months.

### 16.2 Cohesion

```python
def cohesion(cluster: set[str], edges: EdgeIndex, threshold: float) -> float:
    """Fraction of possible internal pairs that actually match.
    Chain of n records: ~2/n. Genuine cluster: near 1.0."""
    n = len(cluster)
    if n < 2:
        return 1.0
    possible = n * (n - 1) // 2
    actual = sum(
        1 for a, b in itertools.combinations(sorted(cluster), 2)
        if edges.score(a, b) >= threshold
    )
    return actual / possible
```

### 16.3 The loop circuit breaker

```python
class LoopDetector:
    """Catches echo loops that suppression missed. This is the backstop,
    and it should alert loudly — a tripped breaker is a bug in §7.2."""

    def __init__(self, max_changes: int = 10, window: timedelta = timedelta(minutes=5)):
        self.max_changes = max_changes
        self.window = window

    def check(self, source: str, record_id: str) -> bool:
        count = self.store.change_count(source, record_id, since=now() - self.window)
        if count > self.max_changes:
            self.store.quarantine(source, record_id, reason="suspected echo loop")
            alert("sync loop detected — record quarantined",
                  source=source, record_id=record_id, count=count)
            return False
        return True
```

---

## 17. Testing

### 17.1 The three tests that matter most

```python
def test_no_supercluster_from_chain():
    """A ← 0.85 → B ← 0.85 → C ← 0.85 → D, with A/D clearly distinct.
    Must NOT become one cluster."""
    records = build_chain(n=8, edge_score=0.85)
    clusters = resolve_clusters(records, config=default_config)
    assert max(len(c) for c in clusters) <= 3, "chain collapsed into a supercluster"


def test_no_echo_loop():
    """24-hour simulated soak with continuous edits on both sides."""
    env = SyncTestEnv(salesforce=FakeSF(), hubspot=FakeHS())
    env.enable_bidirectional(["first_name", "phone"])
    for _ in range(1000):
        env.edit_random_side()
        env.run_sync_cycle()
    assert env.total_writes() < env.total_user_edits() * 2.5, "echo amplification"
    assert env.loop_breaker_trips() == 0


def test_erasure_is_complete():
    entity = seed_entity_everywhere()
    erase(entity.id, reason="gdpr_request")
    for store in ALL_STORES:          # every single one
        assert store.search(entity.id) == [], f"residue in {store.name}"
```

### 17.2 Measure precision on real decisions

The precision number that gates auto-merge must come from adjudicated decisions on your data, not from a benchmark or a paper.

```python
def precision_by_band(decisions: list[ReviewDecision]) -> dict[str, float]:
    """Reviewer decisions grouped by the score the model assigned.
    THIS is what justifies raising or lowering auto_merge_threshold."""
    bands = defaultdict(lambda: {"merge": 0, "split": 0})
    for d in decisions:
        band = score_band(d.model_score)
        bands[band]["merge" if d.human_said_match else "split"] += 1
    return {b: v["merge"] / (v["merge"] + v["split"]) for b, v in bands.items()}
```

Review it after every model change. A threshold set once and never revisited will drift out of calibration as the data changes.

### 17.3 Property tests

- Resolution is deterministic: same observations plus same policy gives the same value
- Event replay is order-independent for final state
- Merging is associative and commutative in your store
- Normalization is idempotent: `normalize(normalize(x)) == normalize(x)`

### 17.4 Golden fixtures for matching

A corpus of hand-labeled pairs — true matches, true non-matches, and the genuinely hard cases (same name different company, same person different email, nicknames, married names, transliterations). Assert on the decision, not the score, so the fixtures survive model retraining.

---

## 18. Stretch goals

| Feature | Effort | Value |
|---|---|---|
| **Active learning for the review queue** | Medium | Surface the pairs where the model is least certain — dramatically more labels per hour of reviewer time |
| **Household / account hierarchy resolution** | Large | Parent-subsidiary company graphs |
| **Explainable match scores** | Small | "Matched on email domain + surname; differed on first name." Drives reviewer speed and trust. |
| **Confidence-weighted field resolution** | Medium | Blend observations rather than picking one |
| **Third CRM or marketing automation** | Medium | The policy model already generalizes |
| **Warehouse as a sync peer** | Medium | Snowflake/BigQuery as another source in the same framework |
| **Data quality scoring per record** | Small | Completeness, freshness, conflict count — surfaces where to spend enrichment budget |
| **Self-tuning thresholds** | Medium | Adjust bands from ongoing review decisions, with guardrails |
| **Unmerge in the CRM** | Large | Hard because CRMs don't support it well — but you have the data to do it (§4.2) |

---

## 19. References

### Record linkage

- **Fellegi & Sunter**, "A Theory for Record Linkage" (JASA, 1969) — the foundation of §5.3
- **Splink** documentation, especially on blocking rules and `m`/`u` estimation
- **Christen**, *Data Matching* — the standard textbook; blocking, comparison functions, evaluation
- **Winkler**, US Census Bureau papers on record linkage in practice
- Double Metaphone (Philips, 2000) — phonetic matching that handles non-English names

### Distributed data

- **Kleppmann**, *Designing Data-Intensive Applications*, ch. 5 and 9 — replication, conflict resolution, why LWW loses data
- CRDT literature — the theoretically clean answer, and why it doesn't apply when the peers are SaaS products you don't control
- **Helland**, "Life Beyond Distributed Transactions" — idempotency and at-least-once as a design stance

### Platform

| Source | For |
|---|---|
| Salesforce API Request Limits and Allocations | The §3.1 numbers; verify against your own org |
| Salesforce Bulk API 2.0 Limits | Job sizes, batch counts, concurrency |
| Salesforce Change Data Capture | Push-based change detection |
| HubSpot API usage guidelines | Burst and daily limits, and the shared-across-apps behavior |
| `pg_trgm` documentation | In-database fuzzy blocking |
| GDPR Arts. 5, 6, 14, 17 | Lawful basis, notice when data comes from third parties, erasure |

---

## Appendix A — Decision record

| Decision | Rationale |
|---|---|
| **Store observations, not values; resolve on read** | Provenance is free, policy changes are replayable, conflicts are visible by construction, and erasure is provable |
| Bitemporal: `observed_at` and `recorded_at` separately | They diverge on delayed webhooks and backfills; conflating them makes ordering wrong |
| **Merges recorded as links, never as deletions** | The CRM can't unmerge; your store can, and §5.4 guarantees you'll need to |
| **False merges and false splits are not symmetric** | A merge is irreversible data destruction and a privacy exposure; a split is a duplicate row |
| **Tune for precision at fixed recall, never F1** | F1 treats the two error types as equal, which is precisely the mistake |
| Auto-merge threshold measured, not chosen | 99% precision sounds excellent and destroys 1,000 records per 100,000 merges |
| **Review queue built before auto-merge** | Reviewer decisions are both the precision measurement and the training labels |
| Blocking is mandatory, with measured recall loss | 1M records is 500 billion pairs; blocking that drops true pairs caps recall permanently |
| Term frequency adjustments on | Agreeing on "Nguyen" is weak evidence; agreeing on a rare surname is strong. Common-name false merges drive superclusters. |
| Double Metaphone over Soundex | Handles non-English names materially better |
| Provider-aware email normalization | Gmail ignores dots; most providers don't. Global dot-stripping creates a whole class of false merges. |
| **Cluster guards: size, cohesion, contradiction** | Matching isn't transitive; connected components collapses chains into superclusters |
| Cohesion threshold ~0.7 | A chain of n has cohesion ~2/n; a real cluster is near 1 |
| Cluster-size distribution monitored after every model change | The earliest warning that a threshold moved |
| **Direction and master declared per field, not per object** | Most fields have an obvious owner; every bidirectional field is a conflict you volunteered for |
| Echo suppression via origin tagging **and** a write log | CRMs don't reliably surface the modifier; the write log doesn't depend on them |
| Loop circuit breaker as a backstop, alerting loudly | A trip means suppression has a bug, and that's the failure you most want visible |
| Polling reconciliation alongside push | Every push mechanism drops events; silent drift is worse than latency |
| Poll `SystemModstamp`, not `LastModifiedDate` | It advances on system-level changes the other misses |
| **LWW only within a single source** | Cross-source LWW depends on clocks you don't control, and "last" isn't "correct" |
| `HUMAN_WINS` as the default for editable fields | An enrichment run overwriting a rep's correction is the fastest way to lose trust |
| Conflicts logged even when auto-resolved | The log tells you whether the policy is right; a high-conflict field usually means the master is wrong |
| **Deletes are soft and never auto-propagated** | Same asymmetry as merges — an unwanted propagated delete is unrecoverable |
| **API budget treated as a shared resource with 30% reserve** | HubSpot's daily cap is shared across every app; Salesforce pools all API types. Exhausting it breaks the customer's whole integration ecosystem. |
| Consumption read from provider headers, not counted locally | Your count misses every other integration on the account |
| Bulk API above ~2,000 records; backfill throttled and pausable | A backfill that can't be paused will take down a customer at quarter end |
| Out-of-order observations stored, not discarded | They're evidence, needed for replay, and may win under a different policy |
| DLQ with depth alerting | An unmonitored dead-letter queue is a data-loss mechanism with extra steps |
| Erasure adds a suppression record | Otherwise the next backfill resurrects the person — exactly what the regulation is about |
| Don't write your own Fellegi-Sunter | Splink is mature and calibrated; the interesting work is everything around it |

---

## Appendix B — Quick reference card

```
SCALE
  pairs = n(n-1)/2
   10k →      50 million        100k →   5 billion
    1M →     500 billion        blocking is MANDATORY

ASYMMETRY (the governing principle)
  false MERGE  → irreversible data loss + privacy exposure
  false SPLIT  → a duplicate row
  99% precision × 100k merges = 1,000 DESTROYED RECORDS
  → tune precision at fixed recall, NEVER F1
  → review queue BEFORE auto-merge; measure, then promote bands

  auto_merge  ≥ 0.995 (measured on YOUR data)
  review      ≥ 0.80
  distinct    < 0.80

CLUSTERING — matching is NOT transitive
  A~B, B~C, A≁C → connected components merges all three
  guards:  max_size 10 (people) / 25 (companies)
           cohesion ≥ 0.7        ← chain of n has ~2/n
           no internal pair below contradiction threshold
  monitor cluster-size distribution after every model change

BIDIRECTIONAL SYNC
  echo loop: SF edit → HS write → HS hook → SF write → SF hook → ∞
  suppress with BOTH:
    origin tag (LastModifiedById / sourceId) — fast, unreliable
    write log (record_id, field, value_hash, 5 min) — robust
  + circuit breaker: >10 changes/5min on one record → quarantine + alert
  poll SystemModstamp as a backstop; push always drops events

CONFLICTS
  LWW across sources = broken (clock skew you don't control,
    and "last" ≠ "correct" — a 3am automation is last for 10k records)
  LWW WITHIN one source = fine (one clock)
  → field-level source-of-truth policy, HUMAN_WINS by default
  → log every conflict, even auto-resolved
  → deletes are soft and never auto-propagate

API BUDGET — you're spending someone else's
  Salesforce EE   100,000/day + 1,000/user, rolling 24h
                  pooled across REST/SOAP/Bulk/Connect
  Salesforce Dev  15,000/day hard cap
  Bulk API 2.0    150MB/job · 10k rec/batch · 15,000 batches/day · 25 concurrent
  HubSpot Pro     190 req/10s · 625k/day     Ent: 190/10s · 1M/day
  HubSpot Search  5 req/SECOND  ← much tighter
  ★ HubSpot daily cap is SHARED ACROSS EVERY APP on the account
  ★ reserve 30% · read usage from response headers · Bulk above 2k records

FRESHNESS
  job_title 90d · company 90d · email 180d · industry 365d
  expired observations excluded from resolution, NOT deleted
```
