# Runbook: a cluster collapsed

## Symptom

A single entity with far more records than a person could plausibly have.  The
characteristic signature: forty email addresses on one record, a job title from
an industry nobody else in the cluster works in, and a surname that is common in
your data.

Usually reported as "the CRM has a record with someone else's data in it",
which is the privacy incident half of the problem.

## Immediate actions

1. **Stop syncing the affected records.**

   ```python
   detector.quarantine("salesforce", record_id, "supercluster under investigation")
   ```

   This stops writes propagating the merged state outward while you work.

2. **Establish the blast radius.**

   ```python
   pipeline.entities.merged_into(entity_id)          # what was folded in
   pipeline.entities.merges_for(entity_id)           # when, by whom, at what score
   provenance(pipeline.observations, entity_id)      # every observation and its source
   ```

   The merge records carry `decided_by` (`auto:v3`, `user:alice@...`) and the
   score. If `decided_by` is an automated version, the model or the threshold is
   at fault; if it is a person, the review UI is showing them too little.

3. **Unmerge.**

   ```python
   pipeline.entities.unmerge(merged_id, "supercluster", by="ops")
   ```

   This works here even though the CRM cannot do it, because merges were
   recorded as links and observations stayed attached to their original entity.
   That is the whole point of section 4.2.

4. **Tell the CRM.** Unmerging in our store does not unmerge in Salesforce.
   Depending on how much was written outward, this may mean recreating records
   from the retained observations.

## Diagnosis

Work down this list; the first one that fires is usually it.

| Check | What it looks like | Fix |
|---|---|---|
| Cohesion | `cohesion(cluster, edges, 0.8)` well below 0.7 | A chain got through. Check `min_cohesion` was not lowered. |
| Cluster size distribution | Compare `size_distribution()` before and after the last model change — `distribution_shift()` renders the alert | Revert the model or the threshold. |
| Term frequency | The shared value is a common surname or a shared company name | Confirm `term_frequency_adjustments=True` and that `fit_term_frequencies()` ran on the *current* corpus. |
| Blocking | An oversized block was processed rather than skipped | Check `BlockingStats.oversized_blocks` and add something selective to the rule. |
| Email normalization | Distinct addresses collapsed into one | Dot-stripping applied off Gmail. Only `DOT_INSENSITIVE_DOMAINS` may lose dots. |
| Role addresses | The joining value is `info@`, `sales@`, `support@` | The record should never have matched on it; check `ROLE_LOCALPARTS`. |
| Phone | The joining value is a shared office or household number | Unverified phone must not be a deterministic key. |
| Threshold | `auto_merge_threshold` was lowered, or a band was promoted | Re-run `promotion_report()`; a band promoted on fewer than `min_decisions_to_promote` decisions is not promoted on evidence. |

## Prevention, after the incident

* Add the collapsed cluster to the golden fixtures as a labelled non-match, and
  assert on the decision rather than the score so it survives retraining.
* Re-measure precision by band from review decisions; if the band that produced
  this merge is still above the bar, the labelled set is not representative.
* Turn the affected band back to review — demoting is one flag, and it is
  cheaper than the next incident.
* Check `max_size`: 10 for people, 25 for companies, and a cluster over it goes
  to review by definition.

## What not to do

**Do not "clean up" by deleting the duplicate rows.**  The observations are the
evidence of what happened and the only route back to the pre-merge state.

**Do not raise `min_cohesion` to 1.0.**  It stops genuine duplicate clusters of
three or more from ever merging, and you will have traded a visible failure for
an invisible one.
