"""Enrichment quality, TTLs and the review queue (sections 10, 5.4, 17.2)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lep.conflict.strategies import FieldPolicy, ResolutionStrategy
from lep.core.types import Observation, Record, SourceKind
from lep.enrich.bakeoff import bake_off, disagreement_matrix
from lep.enrich.providers import StaticProvider, coverage, enrich_entity
from lep.enrich.ttl import FIELD_TTL, expires_at, reenrichment_candidates
from lep.identity.policy import MatchPolicy
from lep.review.queue import ReviewQueue, precision_by_band
from lep.store.resolve import resolve
from lep.testing import EPOCH


@pytest.fixture
def providers():
    accurate = StaticProvider(
        "clearbit_like",
        {
            "ann@acme.com": {"job_title": "VP Engineering", "employee_count": 500},
            "bob@globex.com": {"job_title": "CFO", "employee_count": 1200},
        },
        cost_per_call=2.0,
        confidence=0.9,
    )
    cheap = StaticProvider(
        "cheap_provider",
        {
            "ann@acme.com": {"job_title": "Engineer", "employee_count": 50},
            "bob@globex.com": {"job_title": "Analyst", "employee_count": 300},
        },
        cost_per_call=0.5,
        confidence=0.5,
    )
    return accurate, cheap


@pytest.fixture
def records():
    return [
        Record("salesforce", "1", {"email": "ann@acme.com"}),
        Record("salesforce", "2", {"email": "bob@globex.com"}),
        Record("salesforce", "3", {"email": "nobody@nowhere.com"}),
    ]


GOLD = {
    "ann@acme.com": {"job_title": "VP Engineering", "employee_count": 500},
    "bob@globex.com": {"job_title": "CFO", "employee_count": 1200},
}


def test_bake_off_reports_per_field(providers, records):
    report = bake_off(records, list(providers), GOLD)
    job_title = report.by_field("job_title")
    assert job_title[0].provider == "clearbit_like"
    assert job_title[0].accuracy == 1.0
    assert report.by_field("employee_count")[0].provider == "clearbit_like"


def test_cheaper_provider_with_worse_accuracy_is_more_expensive(providers, records):
    """Cost per correctly-enriched field is the metric that decides.

    The cheap provider is a quarter of the price per call and returns a value
    every time -- and costs more per fact you can actually use.
    """
    report = bake_off(records, list(providers), GOLD)
    accurate, cheap = providers
    assert report.cost[cheap.name] < report.cost[accurate.name]
    assert report.cost_per_correct_field(accurate.name) < report.cost_per_correct_field(
        cheap.name
    )


def test_bake_off_produces_a_source_ranking_for_the_field_policy(providers, records):
    report = bake_off(records, list(providers), GOLD)
    ranking = report.recommended_policy()
    assert ranking["job_title"][0] == "clearbit_like"
    assert "Bake-off" in report.render()


def test_providers_disagree_and_the_matrix_shows_where(providers, records):
    matrix = disagreement_matrix(records, list(providers), "employee_count")
    # Both companies: 500 vs 50 and 1200 vs 300.  High disagreement is where
    # hand-verification pays, and you can see it before you have a gold set.
    assert matrix[("cheap_provider", "clearbit_like")] == 2


def test_coverage_is_not_accuracy(providers, records):
    accurate, _ = providers
    results = [accurate.enrich(r) for r in records]
    assert coverage(results) == pytest.approx(2 / 3)


def test_enrichment_writes_observations_not_values(observations, entities, providers):
    entity = entities.create("person").id
    record = Record("salesforce", "1", {"email": "ann@acme.com"})
    written = enrich_entity(observations, entity, record, list(providers))
    assert len(written) == 4
    assert all(o.source_kind is SourceKind.ENRICHMENT for o in written)
    # Two providers disagreeing is a conflict by construction.
    titles = observations.observations(entity, "job_title")
    assert {o.value for o in titles} == {"VP Engineering", "Engineer"}


def test_enrichment_never_overwrites_a_human(observations, entities, providers):
    """The single fastest way to destroy trust (section 10.4)."""
    entity = entities.create("person").id
    observations.append(
        Observation(
            entity_id=entity,
            field="job_title",
            value="Head of Platform",
            source="salesforce",
            source_kind=SourceKind.USER,
            observed_at=EPOCH - timedelta(days=30),
        )
    )
    enrich_entity(
        observations, entity, Record("salesforce", "1", {"email": "ann@acme.com"}), list(providers)
    )
    policy = FieldPolicy("job_title", strategy=ResolutionStrategy.HUMAN_WINS)
    assert resolve(observations, entity, "job_title", policy).value == "Head of Platform"


def test_ttls_follow_the_decay_table():
    assert FIELD_TTL["job_title"] == timedelta(days=90)
    assert FIELD_TTL["industry"] == timedelta(days=365)
    assert FIELD_TTL["founded_year"] is None
    assert expires_at("founded_year", EPOCH) is None
    assert expires_at("job_title", EPOCH) == EPOCH + timedelta(days=90)


def test_reenrichment_is_prioritised_by_engagement_not_age():
    last = {"a": EPOCH - timedelta(days=400), "b": EPOCH - timedelta(days=200)}
    engagement = {"a": 0, "b": 5}
    candidates = reenrichment_candidates(
        ["a", "b"], last, engagement, at=EPOCH, field="job_title", budget=10
    )
    assert candidates == ["b"], "spend credits where somebody is using the record"


# ------------------------------------------------------------ review queue


def test_everything_routes_through_review_before_auto_merge(db):
    queue = ReviewQueue(db)
    item = queue.enqueue("pair", "salesforce:1", right_key="hubspot:2", score=0.9985)
    assert queue.depth() == 1
    queue.decide(item.id, True, "alice@acme.com")
    assert queue.depth() == 0
    assert queue.labels() == [("salesforce:1", "hubspot:2", True)]


def test_uncertain_pairs_are_reviewed_first(db):
    """Active learning, cheaply: labels per hour of reviewer time."""
    queue = ReviewQueue(db)
    queue.enqueue("pair", "a", right_key="b", score=0.999)
    queue.enqueue("pair", "c", right_key="d", score=0.85)
    assert queue.pending()[0].left_key == "c"


def test_precision_by_band_is_what_justifies_promotion(db):
    queue = ReviewQueue(db)
    for i in range(600):
        item = queue.enqueue("pair", f"a{i}", right_key=f"b{i}", score=0.9995)
        queue.decide(item.id, True, "alice@acme.com")
    for i in range(100):
        item = queue.enqueue("pair", f"c{i}", right_key=f"d{i}", score=0.90)
        queue.decide(item.id, i < 70, "alice@acme.com")

    precision = queue.precision_by_band()
    assert precision["0.999+"] == 1.0
    assert precision["0.90-0.95"] == pytest.approx(0.70)

    report = queue.promotion_report(MatchPolicy())
    assert any(line.startswith("PROMOTE") and "0.999+" in line for line in report)
    assert any(line.startswith("HOLD") and "0.90-0.95" in line for line in report)


def test_no_decisions_means_auto_merge_stays_off(db):
    assert "no adjudicated decisions" in ReviewQueue(db).promotion_report()[0]


def test_precision_by_band_ignores_unscored_items(db):
    queue = ReviewQueue(db)
    item = queue.enqueue("cluster", "a", reason="too_large")
    queue.decide(item.id, False, "alice@acme.com")
    assert precision_by_band(queue.decisions()) == {}
