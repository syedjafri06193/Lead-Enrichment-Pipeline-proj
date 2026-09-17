"""End to end: the milestone ladder in one file (design.md section 15)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lep.cli import main
from lep.cluster.guards import ClusterConfig
from lep.core.types import Record
from lep.identity.policy import MatchPolicy
from lep.pipeline import Pipeline
from lep.testing import labelled_pairs, synthetic_corpus


def test_auto_merge_is_off_by_default(pipeline):
    records, truth = synthetic_corpus(n_people=20)
    outcome = pipeline.match(records, labels=labelled_pairs(records, truth))
    assert outcome.auto_merged == []
    assert pipeline.apply_merges(outcome) == 0, "nothing merges before measurement"
    assert pipeline.review.depth() > 0


def test_an_exact_email_match_alone_does_not_clear_the_auto_merge_bar(clock):
    """Even with auto-merge switched on.

    Exact email equality is *very high* precision, not certainty: shared
    mailboxes, family addresses, recycled corporate accounts and data-entry
    reuse all exist.  Its rule confidence (0.99) sits deliberately below the
    0.995 bar, so it goes to a human.
    """
    pipe = Pipeline(
        clock=clock,
        match_policy=MatchPolicy(auto_merge_enabled=True, auto_merge_threshold=0.995),
    )
    duplicates = [
        Record("salesforce", "1", {"email": "ann@acme.com", "first_name": "Ann", "last_name": "Lee"}),
        Record("hubspot", "2", {"email": "ann@acme.com", "first_name": "Ann", "last_name": "Lee"}),
    ]
    for record in duplicates:
        pipe.ingest_record(record)
    outcome = pipe.match(duplicates, train=False)

    assert pipe.apply_merges(outcome) == 0
    assert pipe.entities.entity_for_record("salesforce", "1") != pipe.entities.entity_for_record(
        "hubspot", "2"
    )
    assert pipe.review.depth() > 0


def test_a_certain_duplicate_merges_once_auto_merge_is_enabled(clock):
    """Same external id from the same source is the one rule that is certain."""
    pipe = Pipeline(
        clock=clock,
        match_policy=MatchPolicy(auto_merge_enabled=True, auto_merge_threshold=0.995),
    )
    duplicates = [
        Record("salesforce", "1", {"email": "ann@acme.com", "external_id": "E-1"}),
        Record("salesforce", "2", {"email": "ann+alias@acme.com", "external_id": "E-1"}),
    ]
    for record in duplicates:
        pipe.ingest_record(record)
    outcome = pipe.match(duplicates, train=False)

    assert pipe.apply_merges(outcome) == 1
    assert pipe.entities.entity_for_record("salesforce", "1") == pipe.entities.entity_for_record(
        "salesforce", "2"
    )


def test_a_weakly_linked_cluster_is_not_merged_even_with_auto_merge_on(clock):
    pipe = Pipeline(
        clock=clock,
        match_policy=MatchPolicy(auto_merge_enabled=True),
        cluster_config=ClusterConfig(),
    )
    ambiguous = [
        Record("salesforce", "1", {"first_name": "J", "last_name": "Smith", "company": "Acme"}),
        Record("hubspot", "2", {"first_name": "John", "last_name": "Smith", "company": "Acme Corp"}),
    ]
    for record in ambiguous:
        pipe.ingest_record(record)
    outcome = pipe.match(ambiguous, train=False)
    assert pipe.apply_merges(outcome) == 0
    assert pipe.entities.entity_for_record("salesforce", "1") != pipe.entities.entity_for_record(
        "hubspot", "2"
    )


def test_review_decisions_train_the_next_model(pipeline):
    records, truth = synthetic_corpus(n_people=20)
    outcome = pipeline.match(records, train=False)
    assert pipeline.review.depth() > 0

    for item in pipeline.review.pending(limit=1000, kind="pair"):
        if item.right_key is None:
            continue
        pipeline.review.decide(
            item.id, truth[item.left_key] == truth[item.right_key], "alice@acme.com"
        )

    # No labels passed: the pipeline picks up the adjudicated decisions.
    retrained = pipeline.match(records, train=True)
    assert pipeline.scorer.trained
    assert pipeline.scorer.training_report["method"] == "supervised"
    assert len(retrained.scored) == len(outcome.scored)


def test_resolved_record_is_a_function_of_observations(pipeline):
    record = Record(
        "salesforce",
        "003X",
        {"email": "ann@acme.com", "first_name": "Ann", "job_title": "VP Eng"},
    )
    entity = pipeline.ingest_record(record)
    resolved = pipeline.record_for(entity)
    assert resolved["email"] == "ann@acme.com"
    assert resolved["job_title"] == "VP Eng"


def test_ttl_expiry_removes_a_value_without_deleting_history(pipeline, clock):
    record = Record("salesforce", "003Y", {"job_title": "VP Eng"})
    entity = pipeline.ingest_record(record)
    assert pipeline.record_for(entity)["job_title"] == "VP Eng"

    clock.advance(timedelta(days=120))  # job_title TTL is 90 days
    assert "job_title" not in pipeline.record_for(entity)
    assert pipeline.observations.count(entity) == 1


def test_health_snapshot_covers_the_things_worth_watching(pipeline):
    health = pipeline.health()
    for key in (
        "entities",
        "observations",
        "conflicts",
        "review_depth",
        "dlq_depth",
        "quarantined",
        "loop_breaker_trips",
        "budget",
    ):
        assert key in health


def test_policy_problems_are_a_startup_gate():
    from lep.conflict.strategies import PolicySet

    broken = PolicySet.from_mapping(
        {
            "objects": {
                "contact": {"fields": {"email": {"strategy": "source_priority"}}}
            }
        }
    )
    with pytest.raises(ValueError, match="field policy problems"):
        Pipeline(policies=broken)


def test_cli_policy_command(capsys):
    assert main(["policy"]) == 0
    out = capsys.readouterr().out
    assert "policy validates" in out


def test_cli_demo_runs(capsys):
    assert main(["demo", "--people", "8", "--edits", "10"]) == 0
    out = capsys.readouterr().out
    assert "auto-merge is OFF" in out
    assert "circuit breaker:     0 trips" in out
    assert "complete        True" in out
