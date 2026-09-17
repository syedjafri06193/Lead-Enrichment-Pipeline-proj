"""The third of the three tests that matter most (design.md section 17.1).

Erasure must reach every store, and the report is the evidence that it did.
"""

from __future__ import annotations

from lep.privacy.erasure import SuppressionList, erase, provenance
from lep.testing import seed_entity_everywhere


def all_stores(pipeline):
    return [
        pipeline.observations,
        pipeline.conflicts,
        pipeline.dlq,
    ]


def test_erasure_is_complete(pipeline):
    entity = seed_entity_everywhere(pipeline)
    report = erase(
        entity.id,
        "gdpr_request",
        db=pipeline.db,
        stores=all_stores(pipeline),
        entity_store=pipeline.entities,
        crm_clients=pipeline.clients,
        identifiers=[entity.email],
    )
    for store in all_stores(pipeline):  # every single one
        assert store.search(entity.id) == [], f"residue in {store.name}"
    assert pipeline.entities.search(entity.id) == []
    assert report.complete


def test_erasure_report_is_the_evidence(pipeline):
    entity = seed_entity_everywhere(pipeline)
    report = erase(
        entity.id,
        "gdpr_request",
        db=pipeline.db,
        stores=all_stores(pipeline),
        entity_store=pipeline.entities,
        crm_clients=pipeline.clients,
        identifiers=[entity.email],
    )
    assert report.deleted["observations"] > 0
    assert {r["source"] for r in report.crm_requests} == {"salesforce", "hubspot"}
    assert entity.email in report.suppression_keys
    assert "complete" in report.render()
    stored = pipeline.db.query("SELECT * FROM erasure_log")
    assert len(stored) == 1


def test_erasure_reaches_the_crms(pipeline):
    entity = seed_entity_everywhere(pipeline)
    erase(
        entity.id,
        "gdpr_request",
        db=pipeline.db,
        stores=all_stores(pipeline),
        entity_store=pipeline.entities,
        crm_clients=pipeline.clients,
    )
    assert not pipeline.clients["salesforce"].exists("003ERASE")
    assert not pipeline.clients["hubspot"].exists("999")


def test_suppression_stops_the_next_backfill_resurrecting_them(pipeline):
    """Without this, the next import undoes the deletion -- which is exactly
    what the regulation is about (section 11.2)."""
    entity = seed_entity_everywhere(pipeline)
    erase(
        entity.id,
        "gdpr_request",
        db=pipeline.db,
        stores=all_stores(pipeline),
        entity_store=pipeline.entities,
        crm_clients=pipeline.clients,
        identifiers=[entity.email],
    )
    suppression = SuppressionList(pipeline.db)
    incoming = ["someone.else@acme.com", entity.email]
    assert suppression.filter(incoming) == ["someone.else@acme.com"]
    assert suppression.contains("salesforce:003ERASE")


def test_provenance_answers_a_subject_access_request(pipeline):
    entity = seed_entity_everywhere(pipeline)
    trail = provenance(pipeline.observations, entity.id)
    sources = {row["source"] for row in trail}
    assert {"salesforce", "hubspot", "enrichment"} <= sources
    job_titles = provenance(pipeline.observations, entity.id, "job_title")
    assert len(job_titles) == 2
    assert all("observed_at" in row and "recorded_at" in row for row in job_titles)


def test_erasure_of_an_unknown_entity_is_a_no_op(pipeline):
    report = erase(
        "does-not-exist",
        "gdpr_request",
        db=pipeline.db,
        stores=all_stores(pipeline),
        entity_store=pipeline.entities,
    )
    assert report.complete
    assert sum(report.deleted.values()) == 0
