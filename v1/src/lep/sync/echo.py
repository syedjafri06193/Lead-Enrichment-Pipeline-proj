"""Echo suppression (design.md section 7.2).

The loop::

    1. user edits Salesforce        -> SF webhook fires
    2. sync writes to HubSpot       -> HubSpot webhook fires
    3. sync writes back to Salesforce -> SF webhook fires
    4. -> 2  (forever)

Two defenses, and you need both.

**Origin tagging** is fast and cheap: was the change made by our own
integration user?  It works when the CRM surfaces the modifier -- and it does
not always.  Some webhook payloads omit it, workflow-triggered changes attribute
to a different user, and formula and roll-up recalculations attribute to
nobody.

**A write log** is robust, because it does not depend on the CRM preserving
anything: we wrote this exact value to this exact field a moment ago, so this
inbound event is our own write coming back.  Values are hashed rather than
stored -- the log gets large, and only equality is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from lep.core.types import DEFAULT_ECHO_WINDOW, CrmEvent, hash_value, utcnow
from lep.store.db import Database, from_iso, to_iso


@dataclass(frozen=True)
class WriteRecord:
    target: str          # "salesforce"
    record_id: str
    field: str
    value_hash: str
    written_at: datetime | None = None


class WriteLog:
    """Every write we make, hashed, with a short retention window."""

    def __init__(
        self,
        db: Database,
        window: timedelta = DEFAULT_ECHO_WINDOW,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.window = window
        self.clock = clock

    def record(self, target: str, record_id: str, field: str, value: Any) -> WriteRecord:
        now = self.clock()
        digest = hash_value(value)
        self.db.execute(
            "INSERT INTO write_log (target, record_id, field, value_hash, written_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (target, record_id, field, digest, to_iso(now)),
        )
        return WriteRecord(target, record_id, field, digest, now)

    def lookup(
        self, target: str, record_id: str, field: str, window: timedelta | None = None
    ) -> list[WriteRecord]:
        since = self.clock() - (window or self.window)
        rows = self.db.query(
            "SELECT * FROM write_log WHERE target = ? AND record_id = ? AND field = ?"
            " AND written_at >= ? ORDER BY written_at DESC",
            (target, record_id, field, to_iso(since)),
        )
        return [
            WriteRecord(
                target=r["target"],
                record_id=r["record_id"],
                field=r["field"],
                value_hash=r["value_hash"],
                written_at=from_iso(r["written_at"]),
            )
            for r in rows
        ]

    def prune(self, older_than: timedelta | None = None) -> int:
        cutoff = self.clock() - (older_than or self.window * 4)
        cur = self.db.execute(
            "DELETE FROM write_log WHERE written_at < ?", (to_iso(cutoff),)
        )
        return cur.rowcount

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM write_log") or 0)

    def delete_for_record(self, source: str, record_id: str) -> int:
        cur = self.db.execute(
            "DELETE FROM write_log WHERE target = ? AND record_id = ?",
            (source, record_id),
        )
        return cur.rowcount


def is_own_write_by_actor(event: CrmEvent, integration_user_id: str) -> bool:
    """Origin tagging: did our own integration user make this change?"""
    if event.source == "salesforce":
        return event.last_modified_by_id == integration_user_id
    if event.source == "hubspot":
        # Property history carries sourceId / sourceType.
        return event.property_source_id == integration_user_id
    return False


def is_echo(event: CrmEvent, log: WriteLog, window: timedelta = DEFAULT_ECHO_WINDOW) -> bool:
    """We wrote this exact value to this exact field recently."""
    recent = log.lookup(event.source, event.record_id, event.field, window)
    digest = hash_value(event.new_value)
    return any(write.value_hash == digest for write in recent)


@dataclass
class EchoStats:
    by_actor: int = 0
    by_write_log: int = 0
    passed: int = 0

    @property
    def suppressed(self) -> int:
        return self.by_actor + self.by_write_log


class EchoSuppressor:
    """Both defenses, in the order that costs least.

    The write log catches what origin tagging misses -- and origin tagging
    misses more than its documentation implies, which is the entire reason this
    class does not offer a "pick one" option.
    """

    def __init__(
        self,
        log: WriteLog,
        integration_user_ids: dict[str, str],
        *,
        window: timedelta = DEFAULT_ECHO_WINDOW,
    ) -> None:
        self.log = log
        self.integration_user_ids = integration_user_ids
        self.window = window
        self.stats = EchoStats()

    def is_echo(self, event: CrmEvent) -> bool:
        actor_id = self.integration_user_ids.get(event.source)
        if actor_id and is_own_write_by_actor(event, actor_id):
            self.stats.by_actor += 1
            return True
        if is_echo(event, self.log, self.window):
            self.stats.by_write_log += 1
            return True
        self.stats.passed += 1
        return False
