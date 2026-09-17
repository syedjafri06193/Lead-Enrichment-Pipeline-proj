"""Dead letter queue (design.md section 9.3).

An event that always fails will retry forever and block everything behind it.
After ``MAX_ATTEMPTS`` it moves here.

**Someone must look at the DLQ.**  A dead-letter queue nobody monitors is a
data-loss mechanism with extra steps, so this one alerts on *depth*, not just
on individual failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from lep.core.types import utcnow
from lep.store.db import Database, dumps, from_iso, loads, to_iso


@dataclass
class DlqEntry:
    key: str
    payload: dict[str, Any]
    error: str
    attempts: int
    created_at: datetime = field(default_factory=utcnow)
    resolved_at: datetime | None = None
    id: int | None = None


class DeadLetterQueue:
    def __init__(
        self,
        db: Database,
        *,
        alert: Callable[[str], None] | None = None,
        depth_alert_threshold: int = 25,
    ) -> None:
        self.db = db
        self.alert = alert or (lambda message: None)
        self.depth_alert_threshold = depth_alert_threshold
        self._depth_alerted = False

    def send(self, key: str, payload: dict[str, Any], error: str, attempts: int) -> DlqEntry:
        entry = DlqEntry(key=key, payload=payload, error=error, attempts=attempts)
        cur = self.db.execute(
            "INSERT INTO dlq (key, payload, error, attempts, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (key, dumps(payload), error, attempts, to_iso(entry.created_at)),
        )
        entry.id = int(cur.lastrowid)
        self.alert(f"event moved to DLQ: key={key} attempts={attempts} error={error}")
        self._check_depth()
        return entry

    def depth(self) -> int:
        return int(
            self.db.scalar("SELECT COUNT(*) FROM dlq WHERE resolved_at IS NULL") or 0
        )

    def entries(self, limit: int = 50) -> list[DlqEntry]:
        rows = self.db.query(
            "SELECT * FROM dlq WHERE resolved_at IS NULL ORDER BY id LIMIT ?", (limit,)
        )
        return [
            DlqEntry(
                id=r["id"],
                key=r["key"],
                payload=loads(r["payload"]) or {},
                error=r["error"],
                attempts=r["attempts"],
                created_at=from_iso(r["created_at"]),
                resolved_at=from_iso(r["resolved_at"]),
            )
            for r in rows
        ]

    def resolve(self, entry_id: int) -> None:
        self.db.execute(
            "UPDATE dlq SET resolved_at = ? WHERE id = ?", (to_iso(utcnow()), entry_id)
        )
        if self.depth() < self.depth_alert_threshold:
            self._depth_alerted = False

    def _check_depth(self) -> None:
        depth = self.depth()
        if depth >= self.depth_alert_threshold and not self._depth_alerted:
            self._depth_alerted = True
            self.alert(
                f"DLQ depth is {depth} (threshold {self.depth_alert_threshold}): "
                "something systematic is failing, not one bad event"
            )

    def search(self, entity_id: str) -> list[DlqEntry]:
        """Erasure has to reach in here too (section 11.2)."""
        return [
            entry
            for entry in self.entries(limit=10_000)
            if entity_id in dumps(entry.payload)
        ]

    def delete_for_entity(self, entity_id: str) -> int:
        removed = 0
        for entry in self.search(entity_id):
            self.db.execute("DELETE FROM dlq WHERE id = ?", (entry.id,))
            removed += 1
        return removed

    @property
    def name(self) -> str:
        return "dlq"
