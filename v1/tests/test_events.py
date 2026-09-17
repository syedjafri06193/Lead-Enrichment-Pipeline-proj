"""Event-driven mechanics (design.md section 9)."""

from __future__ import annotations

import random
from datetime import timedelta

from lep.core.types import CrmEvent, Observation
from lep.conflict.strategies import FieldPolicy, ResolutionStrategy
from lep.events.dlq import DeadLetterQueue
from lep.events.idempotency import IdempotencyStore
from lep.events.ordering import OrderingResult, apply_observation, provisional_entity
from lep.events.processor import EventProcessor, backoff
from lep.store.resolve import resolve
from lep.testing import EPOCH


def event(value: str, *, minutes: int = 0, change_id: str | None = "c1") -> CrmEvent:
    return CrmEvent(
        source="salesforce",
        record_id="003",
        field="Email",
        new_value=value,
        observed_at=EPOCH + timedelta(minutes=minutes),
        change_id=change_id,
    )


def test_duplicate_delivery_is_skipped(db):
    store = IdempotencyStore(db)
    assert store.claim("salesforce:003:c1")
    assert not store.claim("salesforce:003:c1")


def test_idempotency_key_ignores_redelivery_noise():
    """A redelivery differs in delivery timestamp and attempt count only."""
    first = CrmEvent(
        source="hubspot",
        record_id="1",
        field="email",
        new_value="a@acme.com",
        observed_at=EPOCH,
        change_id=None,
        received_at=EPOCH,
        attempt=0,
    )
    redelivery = CrmEvent(
        source="hubspot",
        record_id="1",
        field="email",
        new_value="a@acme.com",
        observed_at=EPOCH,
        change_id=None,
        received_at=EPOCH + timedelta(minutes=5),
        attempt=3,
    )
    assert first.idempotency_key == redelivery.idempotency_key


def test_provider_change_id_is_preferred():
    assert event("a@acme.com").idempotency_key == "salesforce:003:c1"


def test_out_of_order_observation_is_kept_but_does_not_win(observations, entities):
    entity = entities.create("person").id
    newer = Observation(entity, "email", "new@acme.com", "salesforce", EPOCH + timedelta(hours=1))
    older = Observation(entity, "email", "old@acme.com", "salesforce", EPOCH)

    assert apply_observation(observations, newer).result is OrderingResult.APPLIED
    outcome = apply_observation(observations, older)
    assert outcome.result is OrderingResult.SUPERSEDED
    assert outcome.observation.superseded_by is not None

    policy = FieldPolicy("email", strategy=ResolutionStrategy.MOST_RECENT)
    assert resolve(observations, entity, "email", policy).value == "new@acme.com"
    assert observations.count(entity) == 2, "late observations are evidence, not noise"


def test_a_superseded_observation_can_win_under_a_different_policy(observations, entities):
    entity = entities.create("person").id
    apply_observation(
        observations,
        Observation(entity, "job_title", "Newer", "salesforce", EPOCH + timedelta(days=1)),
    )
    apply_observation(
        observations, Observation(entity, "job_title", "Older", "salesforce", EPOCH)
    )
    first_wins = FieldPolicy("job_title", strategy=ResolutionStrategy.NEVER_OVERWRITE)
    assert resolve(observations, entity, "job_title", first_wins).value == "Older"


def test_replay_is_order_independent(observations, entities):
    """M3 exit criterion: replaying the same stream in random order ten times
    produces identical resolved state."""
    entity = entities.create("person").id
    policy = FieldPolicy(
        "email",
        strategy=ResolutionStrategy.SOURCE_PRIORITY,
        source_ranking=("salesforce", "hubspot"),
    )
    stream = [
        Observation(entity, "email", f"v{i}@acme.com", source, EPOCH + timedelta(minutes=i))
        for i, source in enumerate(["salesforce", "hubspot", "salesforce", "hubspot"])
    ]

    results = set()
    for seed in range(10):
        db_obs = observations
        db_obs.delete_for_entity(entity)
        shuffled = list(stream)
        random.Random(seed).shuffle(shuffled)
        for obs in shuffled:
            apply_observation(db_obs, obs)
        results.add(resolve(db_obs, entity, "email", policy).value)
    assert len(results) == 1, f"resolved state depends on arrival order: {results}"


def test_create_after_update_makes_a_provisional_entity(entities):
    entity_id = provisional_entity(entities, "salesforce", "003NEW")
    assert entities.entity_for_record("salesforce", "003NEW") == entity_id
    # The later create finds the same entity rather than making a second one.
    assert provisional_entity(entities, "salesforce", "003NEW") == entity_id


def test_poison_message_goes_to_the_dlq_after_max_attempts(db):
    def always_fails(event: CrmEvent) -> None:
        raise RuntimeError("bad payload")

    processor = EventProcessor(db, always_fails, max_attempts=3)
    for _ in range(3):
        processor.handle(event("a@acme.com"))

    assert processor.dlq.depth() == 1
    assert processor.stats.retried == 2
    assert processor.dlq.entries()[0].error == "bad payload"


def test_dlq_alerts_on_depth_not_just_on_failures(db):
    alerts: list[str] = []
    dlq = DeadLetterQueue(db, alert=alerts.append, depth_alert_threshold=3)
    for i in range(3):
        dlq.send(f"k{i}", {"i": i}, "boom", 5)
    assert any("DLQ depth" in a for a in alerts)
    assert any("systematic" in a for a in alerts)


def test_a_transient_failure_recovers(db):
    calls = {"n": 0}

    def flaky(event: CrmEvent) -> None:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")

    processor = EventProcessor(db, flaky)
    assert not processor.handle(event("a@acme.com"))
    assert processor.handle(event("a@acme.com"))
    assert processor.dlq.depth() == 0


def test_backoff_is_bounded_and_jittered():
    assert backoff(1, jitter=False) == 1.0
    assert backoff(10, jitter=False) == 300.0
    assert 0 <= backoff(4) <= 8.0
