# Matching: thresholds and measured precision

## The governing arithmetic

```
destroyed_records(precision, merges) = (1 - precision) x merges

  0.99   x 100,000 merges  ->  1,000 records destroyed
  0.995  x 100,000 merges  ->    500
  0.999  x 100,000 merges  ->    100
  0.9999 x 100,000 merges  ->     10
```

A false merge is irreversible in the CRM, silently loses one record's values,
and puts one person's data inside another person's record.  A false split is a
duplicate row somebody notices and fixes.  These are not the same error, and any
metric that treats them as the same — F1, most obviously — is the wrong metric.

**Tune for precision at a fixed recall.**  `precision_at_fixed_recall()` in
`lep.identity.policy` is the function to use.

## Bands

```python
MatchPolicy(
    auto_merge_threshold=0.995,     # measured, not chosen
    review_threshold=0.80,
    auto_merge_enabled=False,       # until measurement says otherwise
    min_decisions_to_promote=500,
    required_precision=0.999,
)
```

| Score | Action |
|---|---|
| >= `auto_merge_threshold` | auto-merge **if that band has been promoted**, otherwise review |
| >= `review_threshold` | review |
| < `review_threshold` | distinct, no action |

`auto_merge_enabled` ships False.  A new deployment sends everything to review,
which is slow, and correct: the queue is how the precision number gets made.

## Promoting a band

`ReviewQueue.promotion_report()` prints one line per score band:

```
PROMOTE  0.999+: precision 1.00000 over 600 decisions
HOLD     0.90-0.95: measured precision 0.70000 is below 0.99900 -- that is
         30,000 destroyed records per 100,000 merges
```

Two conditions, both necessary: enough adjudicated decisions for the estimate to
mean anything, and a measured precision above the bar.  "The model scored these
0.998" is not evidence.  "Reviewers agreed with 600 of 600 in this band" is.

Re-run it after every model change.  A threshold set once and never revisited
drifts out of calibration as the data changes.

## What is measured, and where

| Number | Function | Why it matters |
|---|---|---|
| Blocking recall | `measure_recall()` | A hard ceiling on the whole pipeline. A true pair blocking never generates cannot be scored, reviewed or merged. |
| Reduction ratio | `BlockingStats.reduction_ratio` | Whether this runs at all. 1M records is 500 billion pairs. |
| Oversized blocks | `BlockingStats.oversized_blocks` | A block that explodes is a bug in the rule, not a slow day. |
| Marginal rule recall | `rule_contribution()` | A rule contributing nothing is pure cost. |
| Precision by band | `ReviewQueue.precision_by_band()` | The only input to the auto-merge decision. |
| Cluster size distribution | `ClusterResult.size_distribution()` | Compare after every model change; a shift toward large clusters is the earliest warning you get. |

## Deterministic rules and their confidence

| Rule | Confidence | Note |
|---|---|---|
| Same external id, same source | 1.00 | Certain. |
| Normalized email, excluding role addresses | 0.99 | Very high, and deliberately below the 0.995 auto-merge bar: shared mailboxes, family addresses and recycled corporate accounts all exist. |
| LinkedIn profile URL | 0.97 | High. |
| Verified phone + surname | 0.95 | High. Unverified phone is not used at all — shared office and household numbers make it a false-merge source. |

`info@`, `sales@`, `support@` and friends are excluded from the email rule
entirely.  They identify a function, not a person, and merging on them joins
everyone who ever emailed from the company inbox.

## Normalization, and the mistake to avoid

Gmail ignores dots in the local part.  Most other providers do not.  Strip dots
globally and `j.smith@company.com` merges with `jsmith@company.com` at a domain
where those are two different people — a class of false merges nobody traces
back for months.  `+tag` stripping is broadly safe; dot stripping is not.

Double Metaphone rather than Soundex, because it handles non-English names
materially better and real contact data is full of them.

## Term frequency adjustments

Two records agreeing on the surname "Nguyen" is weak evidence; agreeing on
"Featherstonehaugh" is strong.  Without the adjustment both count the same, and
common-name agreement is a leading cause of the super-cluster problem.  On in
`lep.identity.scorer` for every name, email, company and phone comparison.

## Training

| Method | When |
|---|---|
| `fit_supervised(records, labels)` | Preferred. By the time auto-merge is under discussion, review decisions *are* labels. |
| `fit_em(records, pairs)` | Cold start, no labels. Estimates m, u and the prior by EM under conditional independence — an assumption that is wrong in detail, which is one more reason the output has to be calibrated against review decisions before it gates anything. |

The estimated prior is capped at 0.5.  A blocked candidate set is heavily
enriched with true duplicates, and a model that believes most pairs match is one
threshold away from a catastrophe.

## Golden fixtures

`lep.testing.synthetic_corpus()` includes the hard cases on purpose: nicknames
(Bill/William), married and cased surnames, transliterations, the same name at
different companies, and the same person at different addresses.  Assert on the
*decision*, not the score, so the fixtures survive retraining.
