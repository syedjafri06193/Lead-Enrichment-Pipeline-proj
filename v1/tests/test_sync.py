"""Sync direction, schema drift, reconciliation and deletes (sections 7, 8.4, 12)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lep.core.types import CrmEvent
from lep.sync.engine import CrmMapping
from lep.sync.reconcile import Reconciler, reconcile
from lep.sync.salesforce import FieldMetadata, MappingVersion, SchemaDriftError, batched
from lep.testing import EPOCH, SyncTestEnv


@pytest.fixture
def env() -> SyncTestEnv:
    return SyncTestEnv()


def test_direction_is_per_field_not_per_object(env):
    """``owner_id`` is sf_only: it is never written outward."""
    env.salesforce.user_edit("003AAA", "OwnerId", "005NEW")
    env.run_sync_cycle()
    assert "owner_id" not in env.hubspot.records["101"].fields
    assert env.resolved("owner_id").value == "005NEW"


def test_one_directional_field_flows_one_way(env):
    env.hubspot.user_edit("101", "lifecyclestage", "customer")
    env.run_sync_cycle()
    # lifecycle_stage is hubspot_to_sf, but Salesforce has no mapping for it,
    # so nothing is written and nothing is silently mangled.
    assert env.resolved("lifecycle_stage").value == "customer"


def test_bidirectional_field_reaches_the_other_side(env):
    env.salesforce.user_edit("003AAA", "FirstName", "Annabel")
    env.run_sync_cycle()
    assert env.hubspot.records["101"].fields["firstname"] == "Annabel"


def test_a_no_op_write_is_never_made(env):
    env.salesforce.user_edit("003AAA", "FirstName", "Ann")  # unchanged value
    env.run_sync_cycle()
    assert env.engine.stats.writes == 0


def test_budget_exhaustion_defers_rather_than_corrupting(env):
    env.budget.budget("hubspot").consumed = env.budget.budget("hubspot").usable
    env.salesforce.user_edit("003AAA", "FirstName", "Annabel")
    env.run_sync_cycle()
    assert env.engine.stats.budget_denied >= 1
    assert "firstname" not in env.hubspot.records["101"].fields or (
        env.hubspot.records["101"].fields["firstname"] == "Ann"
    )


def test_quarantined_records_stop_syncing(env):
    env.engine.loop_detector.quarantine("hubspot", "101", "manual")
    env.salesforce.user_edit("003AAA", "FirstName", "Annabel")
    env.run_sync_cycle()
    assert env.hubspot.records["101"].fields.get("firstname") == "Ann"


def test_deletes_go_to_review_and_are_never_propagated(env):
    env.engine.ingest_delete("salesforce", "003AAA")
    assert env.engine.stats.deletes_to_review == 1
    pending = env.review.pending(kind="delete")
    assert len(pending) == 1
    assert env.hubspot.exists("101"), "a delete must not propagate on its own"


def test_dropped_webhooks_are_recovered_by_reconciliation():
    """Every push mechanism drops events; the poll is the backstop."""
    env = SyncTestEnv(drop_webhook_rate=1.0)  # nothing is delivered
    env.salesforce.user_edit("003AAA", "FirstName", "Annabel")
    env.run_sync_cycle()
    assert env.engine.stats.events_in == 0

    report = reconcile(env.engine, "salesforce", EPOCH - timedelta(days=1))
    assert report.discrepancies
    assert report.repaired >= 1
    assert env.resolved("first_name").value == "Annabel"


def test_reconciliation_uses_system_modstamp_not_last_modified():
    env = SyncTestEnv(drop_webhook_rate=1.0)
    env.clock.advance(timedelta(hours=1))
    env.salesforce.records["003AAA"].fields["FirstName"] = "SystemChanged"
    env.salesforce.system_touch("003AAA")
    report = reconcile(env.engine, "salesforce", EPOCH)
    assert any(field == "FirstName" for _, field, _, _ in report.discrepancies)


def test_rising_discrepancy_rate_alerts():
    env = SyncTestEnv(drop_webhook_rate=1.0)
    reconciler = Reconciler(env.engine)
    reconciler.run("salesforce", now=env.clock())
    for i in range(5):
        env.salesforce.user_edit("003AAA", "FirstName", f"Drift{i}")
        env.clock.advance(timedelta(minutes=5))
    report = reconciler.run("salesforce", now=env.clock())
    assert report.discrepancy_rate > 0


def test_schema_drift_fails_loudly():
    metadata = {"Email": FieldMetadata("Email"), "FirstName": FieldMetadata("FirstName")}
    mapping = MappingVersion("v3", {"email": "Email", "job_title": "Title"})
    with pytest.raises(SchemaDriftError) as excinfo:
        mapping.validate(metadata)
    assert "Title" in str(excinfo.value)


def test_invalid_picklist_value_is_rejected_at_write_time(env):
    with pytest.raises(SchemaDriftError):
        env.salesforce.write("003AAA", "LeadSource", "Carrier Pigeon")
    env.salesforce.add_picklist_value("LeadSource", "Carrier Pigeon")
    env.salesforce.write("003AAA", "LeadSource", "Carrier Pigeon")


def test_a_rename_orphans_the_mapping_and_is_detectable(env):
    env.salesforce.rename_field("Title", "JobTitle__c")
    mapping = MappingVersion("v1", {"job_title": "Title"})
    with pytest.raises(SchemaDriftError):
        mapping.validate(env.salesforce.describe())


def test_unmapped_inbound_fields_are_counted_not_crashed(env):
    env.engine.ingest(
        CrmEvent(
            source="salesforce",
            record_id="003AAA",
            field="Custom_Field__c",
            new_value="x",
            observed_at=env.clock(),
            change_id="x1",
        )
    )
    assert env.engine.stats.unmapped == 1


def test_batched_reads_cost_one_call_per_200():
    assert len(batched([str(i) for i in range(500)])) == 3


def test_mapping_round_trips():
    mapping = CrmMapping("salesforce", {"email": "Email"})
    assert mapping.remote("email") == "Email"
    assert mapping.canonical("Email") == "email"
    assert mapping.canonical("Nope") is None
