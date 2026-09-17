"""The loop circuit breaker (design.md sections 7.2, 16.3).

This is the backstop for echo loops that suppression missed, and it should
alert loudly: **a tripped breaker is a bug in the suppression**, and that is the
failure you most want visible rather than absorbed.

A quarantined record stops syncing until a human clears it.  Stopping is the
safe direction: an un-synced record is stale, a looping record burns the
customer's API allocation and rewrites their data hundreds of times an hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from lep.core.types import utcnow
from lep.store.db import Database, to_iso


class LoopDetector:
    def __init__(
        self,
        db: Database,
        *,
        max_changes: int = 10,
        window: timedelta = timedelta(minutes=5),
        alert: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.max_changes = max_changes
        self.window = window
        self.alert = alert or (lambda message: None)
        #: Injectable so a soak test can run 24 simulated hours in a second.
        self.clock = clock
        self.trips = 0

    def observe(self, source: str, record_id: str, field: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO change_log (source, record_id, field, seen_at) VALUES (?, ?, ?, ?)",
            (source, record_id, field, to_iso(self.clock())),
        )

    def change_count(self, source: str, record_id: str) -> int:
        since = self.clock() - self.window
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM change_log WHERE source = ? AND record_id = ?"
                " AND seen_at >= ?",
                (source, record_id, to_iso(since)),
            )
            or 0
        )

    def check(self, source: str, record_id: str) -> bool:
        """False means: stop syncing this record.

        Call this on every inbound change, *before* doing any work with it.
        """
        if self.is_quarantined(source, record_id):
            return False
        count = self.change_count(source, record_id)
        if count > self.max_changes:
            self.quarantine(source, record_id, "suspected echo loop")
            self.trips += 1
            self.alert(
                f"sync loop detected -- record quarantined: source={source} "
                f"record_id={record_id} changes={count} in {self.window}"
            )
            return False
        return True

    def quarantine(self, source: str, record_id: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO quarantine (source, record_id, reason, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (source, record_id) DO UPDATE SET reason = excluded.reason,"
            " created_at = excluded.created_at, released_at = NULL",
            (source, record_id, reason, to_iso(self.clock())),
        )

    def is_quarantined(self, source: str, record_id: str) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM quarantine WHERE source = ? AND record_id = ?"
                " AND released_at IS NULL",
                (source, record_id),
            )
            is not None
        )

    def release(self, source: str, record_id: str) -> None:
        self.db.execute(
            "UPDATE quarantine SET released_at = ? WHERE source = ? AND record_id = ?",
            (to_iso(self.clock()), source, record_id),
        )

    def quarantined(self) -> list[tuple[str, str, str]]:
        rows = self.db.query(
            "SELECT source, record_id, reason FROM quarantine WHERE released_at IS NULL"
        )
        return [(r["source"], r["record_id"], r["reason"]) for r in rows]
