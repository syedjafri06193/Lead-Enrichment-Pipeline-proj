"""Observation store and resolution (design.md sections 4, 8)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lep.conflict.log import ConflictLog
from lep.conflict.strategies import (
    Direction,
    FieldPolicy,
    PolicySet,
    ResolutionStrategy,
)
from lep.core.types import Observation, SourceKind
from lep.store.resolve import resolve
from lep.testing import EPOCH


def obs(entity, field, value, source, *, days_ago=0, kind=SourceKind.CRM, conf=1.0, ttl=None):
    observed = EPOCH - timedelta(days=days_ago)
    return Observation(
        entity_id=entity,
        field=field,
        value=value,
        source=source,
        source_kind=kind,
        observed_at=observed,
        recorded_at=observed,
        confidence=conf,
        expires_at=observed + ttl if ttl else None,
    )


@pytest.fixture
def entity(entities):
    return entities.create("person").id


def test_nothing_is_ever_overwritten(observations, entity):
    observations.append(obs(entity, "email", "old@acme.com", "salesforce", days_ago=2))
    observations.append(obs(entity, "email", "new@acme.com", "salesforce"))
    history = observations.observations(entity, "email")
    assert [o.value for o in history] == ["new@acme.com", "old@acme.com"]


def test_policy_change_is_replayable(observations, entity):
    """The M1 exit criterion: changing a policy and re-resolving produces a
    different current value from the same history, with nothing lost."""
    observations.append(obs(entity, "job_title", "VP Eng", "salesforce", days_ago=1))
    observations.append(
        obs(entity, "job_title", "Director", "enrichment", kind=SourceKind.ENRICHMENT)
    )

    sf_first = FieldPolicy(
        "job_title",
        strategy=ResolutionStrategy.SOURCE_PRIORITY,
        source_ranking=("salesforce", "enrichment"),
    )
    enrichment_first = FieldPolicy(
        "job_title",
        strategy=ResolutionStrategy.SOURCE_PRIORITY,
        source_ranking=("enrichment", "salesforce"),
    )

    assert resolve(observations, entity, "job_title", sf_first).value == "VP Eng"
    assert resolve(observations, entity, "job_title", enrichment_first).value == "Director"
    assert observations.count(entity) == 2  # nothing lost either way


def test_human_wins_beats_enrichment(observations, entity):
    """Section 10.4: enrichment fills gaps, it does not correct people."""
    observations.append(
        obs(entity, "job_title", "Head of Platform", "salesforce", days_ago=3, kind=SourceKind.USER)
    )
    observations.append(
        obs(entity, "job_title", "Director", "enrichment", kind=SourceKind.ENRICHMENT)
    )
    policy = FieldPolicy("job_title", strategy=ResolutionStrategy.HUMAN_WINS)
    resolved = resolve(observations, entity, "job_title", policy)
    assert resolved.value == "Head of Platform"
    assert resolved.reason == "human edit"


def test_lww_within_one_source_is_fine(observations, entity):
    observations.append(obs(entity, "phone", "+14155550100", "salesforce", days_ago=2))
    observations.append(obs(entity, "phone", "+14155550199", "salesforce"))
    policy = FieldPolicy("phone", strategy=ResolutionStrategy.MOST_RECENT)
    resolved = resolve(observations, entity, "phone", policy)
    assert resolved.value == "+14155550199"
    assert not resolved.needs_review


def test_lww_across_sources_escalates(observations, entity):
    """Clock skew across systems you do not control: not orderable."""
    observations.append(obs(entity, "phone", "+14155550100", "salesforce"))
    observations.append(obs(entity, "phone", "+14155550199", "hubspot"))
    policy = FieldPolicy("phone", strategy=ResolutionStrategy.MOST_RECENT)
    resolved = resolve(observations, entity, "phone", policy)
    assert resolved.needs_review
    assert "not comparable" in resolved.reason or "ranking" in resolved.reason


def test_conflicts_are_visible_by_construction(observations, entity, db):
    observations.append(obs(entity, "email", "a@acme.com", "salesforce"))
    observations.append(obs(entity, "email", "b@acme.com", "hubspot"))
    log = ConflictLog(db)
    policy = FieldPolicy(
        "email",
        strategy=ResolutionStrategy.SOURCE_PRIORITY,
        source_ranking=("salesforce", "hubspot"),
    )
    resolved = resolve(observations, entity, "email", policy, conflict_log=log)
    assert resolved.value == "a@acme.com"
    assert resolved.is_conflicted
    assert log.top_conflicting_fields() == [("email", 1)]
    assert log.for_entity(entity)[0].sources == ["hubspot", "salesforce"]


def test_expired_observations_are_excluded_but_not_deleted(observations, entity):
    observations.append(
        obs(entity, "job_title", "Old Title", "enrichment", days_ago=200, ttl=timedelta(days=90))
    )
    policy = FieldPolicy("job_title", strategy=ResolutionStrategy.SOURCE_PRIORITY)
    resolved = resolve(observations, entity, "job_title", policy, at=EPOCH)
    assert resolved.value is None
    assert observations.count(entity) == 1  # still history, still evidence


def test_as_of_answers_what_did_we_believe_then(observations, entity):
    first = obs(entity, "email", "old@acme.com", "salesforce", days_ago=10)
    observations.append(first)
    policy = FieldPolicy("email", strategy=ResolutionStrategy.SOURCE_PRIORITY)
    later = obs(entity, "email", "new@acme.com", "salesforce")
    observations.append(later)

    then = resolve(observations, entity, "email", policy, as_of=EPOCH - timedelta(days=5))
    now = resolve(observations, entity, "email", policy)
    assert then.value == "old@acme.com"
    assert now.value == "new@acme.com"


def test_never_overwrite_keeps_the_first_value(observations, entity):
    observations.append(obs(entity, "founded_year", 1999, "enrichment", days_ago=5))
    observations.append(obs(entity, "founded_year", 2001, "enrichment"))
    policy = FieldPolicy("founded_year", strategy=ResolutionStrategy.NEVER_OVERWRITE)
    assert resolve(observations, entity, "founded_year", policy).value == 1999


def test_most_confident(observations, entity):
    observations.append(obs(entity, "employee_count", 500, "provider_a", conf=0.4))
    observations.append(obs(entity, "employee_count", 900, "provider_b", conf=0.9))
    policy = FieldPolicy("employee_count", strategy=ResolutionStrategy.MOST_CONFIDENT)
    assert resolve(observations, entity, "employee_count", policy).value == 900


def test_manual_always_escalates(observations, entity):
    observations.append(obs(entity, "owner_id", "005A", "salesforce"))
    policy = FieldPolicy("owner_id", strategy=ResolutionStrategy.MANUAL)
    assert resolve(observations, entity, "owner_id", policy).needs_review


def test_resolution_is_deterministic(observations, entity):
    """Property test (section 17.3)."""
    observations.append(obs(entity, "email", "a@acme.com", "salesforce"))
    observations.append(obs(entity, "email", "b@acme.com", "hubspot"))
    policy = FieldPolicy(
        "email", strategy=ResolutionStrategy.SOURCE_PRIORITY, source_ranking=("hubspot",)
    )
    values = {resolve(observations, entity, "email", policy).value for _ in range(20)}
    assert values == {"b@acme.com"}


def test_merges_are_recorded_not_destructive(entities, observations):
    a = entities.create("person").id
    b = entities.create("person").id
    entities.link(b, "hubspot", "101")
    observations.append(obs(b, "email", "b@acme.com", "hubspot"))

    entities.merge(a, b, score=0.999)
    assert entities.canonical(b) == a
    assert observations.count(b) == 1, "the merged entity keeps its observations"
    assert any(link.source_record_id == "101" for link in entities.links_for(a))

    assert entities.unmerge(b, "false merge found in review")
    assert entities.canonical(b) == b
    assert entities.merges_for(b)[0].reverted_at is not None


def test_merge_cycles_are_refused(entities):
    a, b = entities.create("person").id, entities.create("person").id
    entities.merge(a, b)
    with pytest.raises(Exception):
        entities.merge(b, a)


def test_field_policy_file_validates():
    policies = PolicySet.load("config/field-policy.yaml")
    assert policies.validate() == []
    assert policies.for_field("owner_id").direction is Direction.SF_ONLY
    assert "lifecycle_stage" in policies.synced_fields("salesforce")
    assert "owner_id" not in policies.synced_fields("hubspot")


def test_policy_validation_catches_cross_source_lww():
    bad = PolicySet.from_mapping(
        {
            "objects": {
                "contact": {
                    "fields": {
                        "phone": {"direction": "bidirectional", "strategy": "most_recent"}
                    }
                }
            }
        }
    )
    problems = bad.validate()
    assert problems and "clocks you do not control" in problems[0]
