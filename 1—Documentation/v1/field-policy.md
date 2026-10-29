# Field policy

**Milestone M0.  This document and `config/field-policy.yaml` come before any
sync code, and the data owner signs them off.**

Every field in scope needs four decisions:

1. **master** — which system is authoritative;
2. **direction** — where the value is allowed to flow;
3. **strategy** — how competing observations are resolved;
4. **TTL** — how long a value stays believable.

Most fields should end up one-directional.  Every bidirectional field is a
conflict you have volunteered to resolve, and the reverse-ETL category built
successful products on refusing to take that on at all (design section 2.3).

The policy is validated at startup.  A field declaring cross-source
last-write-wins, or `source_priority` with no ranking, is a startup failure
rather than a surprise in production — see `PolicySet.validate()`.

## Contact

| Field | Master | Direction | Strategy | TTL | Why |
|---|---|---|---|---|---|
| `email` | salesforce | sf → hubspot | source_priority | 180d | Salesforce is where sales operations correct addresses. One-directional because two systems inventing email addresses for the same person is a merge problem, not a sync problem. |
| `first_name` | salesforce | bidirectional | human_wins | — | Genuinely edited in both. Bidirectional is a deliberate cost, taken because reps fix names wherever they happen to be. |
| `last_name` | salesforce | bidirectional | human_wins | — | As above. Married names and transliterations mean this changes for real. |
| `phone` | salesforce | bidirectional | human_wins | — | Edited in both; a human correction beats any automated source. |
| `job_title` | enrichment | enrichment → all | human_wins, then source_priority | 90d | Enrichment fills the gap, a rep's correction wins forever after. A 90-day TTL because job changes are the dominant decay (section 10.3). |
| `lifecycle_stage` | hubspot | hubspot → sf | source_priority | — | Marketing owns the funnel definition. Salesforce receives it; it never writes it back. |
| `owner_id` | salesforce | sf_only | source_priority | — | Territory and ownership are Salesforce concepts. Never written from outside — an integration reassigning account ownership is a very bad afternoon. |
| `notes` | — | none | never_overwrite | — | Deliberately not synced. Free text merges badly and nobody agrees what "the same note" means. |

## Company

| Field | Master | Direction | Strategy | TTL | Why |
|---|---|---|---|---|---|
| `name` | salesforce | bidirectional | human_wins | — | Legal versus trading name is a judgement call a human makes (section 5.5). |
| `domain` | salesforce | sf → hubspot | source_priority | — | The strongest company identifier there is; worth having exactly one owner. |
| `employee_count` | enrichment | enrichment → all | most_confident | 180d | Providers disagree substantially; confidence is the only tiebreak that means anything. |
| `industry` | enrichment | enrichment → all | most_confident | 365d | Changes rarely; disagreement is about taxonomy, not fact. |
| `founded_year` | enrichment | enrichment → all | never_overwrite | none | Immutable. The first non-null value sticks, and no TTL. |

## The strategies

| Strategy | When to use it |
|---|---|
| `source_priority` | The default. Ranked sources; within one source, most-recent wins — same clock, so recency is meaningful. |
| `human_wins` | Every human-editable field. A person who corrected a record did it for a reason, and an enrichment run overwriting it the next morning is the fastest way to lose trust in the system. |
| `most_confident` | Fields where several providers guess and one of them says how sure it is. |
| `never_overwrite` | Immutable facts. |
| `most_recent` | Only where all observations come from one source. Across sources it depends on clocks you do not control, and "last" is not "correct" — a 3am automation is last for ten thousand records. |
| `manual` | Fields too costly to get wrong. Always escalates. |

## Mirrors

Direction also governs *resolution*, not just writes.  Under `sf_to_hubspot`,
the HubSpot copy of the field is our own write coming back; letting it win
would launder our write into a fact.  So the mirror side is not eligible to be
the resolved value — with one exception, which is a human edit.  A person
typing into HubSpot is never a mirror, so `SourceKind.USER` observations are
always eligible regardless of direction.

## Changing the policy

Because the store keeps observations rather than values, a policy change is
replayable: change the ranking, re-resolve, and the current value changes with
nothing lost.  That is the M1 exit criterion and it is tested in
`tests/test_store_and_resolution.py::test_policy_change_is_replayable`.

The signal that a policy is wrong is the conflict log.  Run
`ConflictLog.top_conflicting_fields()` weekly: a field at the top of that list
usually has the wrong master, or two teams editing the same thing in two
systems — an organisational problem the data can surface.
