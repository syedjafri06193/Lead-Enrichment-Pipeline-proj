"""The second of the three tests that matter most (design.md section 17.1).

Bidirectional sync without echo suppression is an infinite loop, and it is the
first thing you will build wrong.  This is the 24-hour soak, run against a
simulated clock.
"""

from __future__ import annotations

from datetime import timedelta

from lep.core.types import CrmEvent, utcnow
from lep.store.db import Database
from lep.sync.echo import EchoSuppressor, WriteLog, is_own_write_by_actor
from lep.testing import SyncTestEnv


def test_no_echo_loop():
    """24-hour simulated soak with continuous edits on both sides."""
    env = SyncTestEnv()
    env.enable_bidirectional(["first_name", "phone"])
    for _ in range(1000):
        env.edit_random_side()
        env.run_sync_cycle()
    assert env.total_writes() < env.total_user_edits() * 2.5, "echo amplification"
    assert env.loop_breaker_trips() == 0


def test_write_log_catches_what_origin_tagging_misses():
    """Attribution is lost often enough that one defense is not enough."""
    env = SyncTestEnv(attribution_loss_rate=1.0)  # the CRM never tells us who
    env.enable_bidirectional(["first_name"])
    for _ in range(100):
        env.edit_random_side()
        env.run_sync_cycle()
    stats = env.engine.echo.stats
    assert stats.by_actor == 0, "attribution was supposed to be unavailable"
    assert stats.by_write_log > 0, "the write log must catch these"
    assert env.loop_breaker_trips() == 0


def test_origin_tagging_alone_is_cheap_when_it_works():
    env = SyncTestEnv(attribution_loss_rate=0.0)
    env.enable_bidirectional(["first_name"])
    for _ in range(50):
        env.edit_random_side()
        env.run_sync_cycle()
    assert env.engine.echo.stats.by_actor > 0
    assert env.loop_breaker_trips() == 0


def test_circuit_breaker_trips_when_suppression_fails():
    """The backstop has to actually work, so it is tested by breaking the
    suppression deliberately.  A tripped breaker is a bug in section 7.2 --
    which is exactly why it alerts."""
    env = SyncTestEnv()
    env.enable_bidirectional(["first_name"])
    env.engine.echo.is_echo = lambda event: False  # sabotage both defenses
    for _ in range(40):
        env.salesforce.user_edit("003AAA", "FirstName", f"Ann{_}")
        env.run_sync_cycle(max_rounds=3)
    assert env.loop_breaker_trips() > 0
    assert any("sync loop detected" in a for a in env.alerts)


def test_is_own_write_by_actor_per_provider():
    sf_event = CrmEvent(
        source="salesforce",
        record_id="003",
        field="Email",
        new_value="a@b.com",
        observed_at=utcnow(),
        last_modified_by_id="integration",
    )
    hs_event = CrmEvent(
        source="hubspot",
        record_id="1",
        field="email",
        new_value="a@b.com",
        observed_at=utcnow(),
        property_source_id="integration",
    )
    assert is_own_write_by_actor(sf_event, "integration")
    assert is_own_write_by_actor(hs_event, "integration")
    assert not is_own_write_by_actor(sf_event, "someone-else")
    # A provider we do not know how to attribute must not be assumed ours.
    other = CrmEvent(
        source="pipedrive",
        record_id="1",
        field="email",
        new_value="a@b.com",
        observed_at=utcnow(),
    )
    assert not is_own_write_by_actor(other, "integration")


def test_write_log_window_expires(db: Database):
    """An echo arriving a week later is somebody's real edit, not our write."""
    now = utcnow()
    moment = {"t": now}
    log = WriteLog(db, window=timedelta(minutes=5), clock=lambda: moment["t"])
    log.record("salesforce", "003", "Email", "a@acme.com")
    suppressor = EchoSuppressor(log, {"salesforce": "integration"})

    event = CrmEvent(
        source="salesforce",
        record_id="003",
        field="Email",
        new_value="a@acme.com",
        observed_at=now,
        last_modified_by_id="a-human",
    )
    assert suppressor.is_echo(event) is True

    moment["t"] = now + timedelta(hours=1)
    assert suppressor.is_echo(event) is False


def test_hashes_not_values_are_stored(db: Database):
    log = WriteLog(db)
    record = log.record("hubspot", "1", "email", "secret@acme.com")
    stored = db.query("SELECT * FROM write_log")[0]
    assert stored["value_hash"] == record.value_hash
    assert "secret@acme.com" not in dict(stored).values()
