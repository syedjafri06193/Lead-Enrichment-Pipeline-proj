"""At-least-once means idempotent everything (design.md section 9.1).

Every webhook provider redelivers.  Every queue redelivers.  Design for it
rather than hoping.

Where the provider gives a stable change identifier, use it; where it does
not, hash the meaningful payload -- carefully excluding fields that vary
between redeliveries (delivery timestamps, attempt counters), because
otherwise every redelivery looks new and the idempotency check does nothing.
That exclusion lives in :attr:`lep.core.types.CrmEvent.idempotency_key`.
"""

from __future__ import annotations

from datetime import timedelta

from lep.core.types import utcnow
from lep.store.db import Database, to_iso


class IdempotencyStore:
    def __init__(self, db: Database, ttl: timedelta = timedelta(days=7)) -> None:
        self.db = db
        self.ttl = ttl

    def claim(self, key: str, ttl: timedelta | None = None) -> bool:
        """True if this is the first time we have seen ``key``.

        Expired claims are reclaimable: the TTL bounds the table, and a
        redelivery a week later is vanishingly unlikely to be a duplicate we
        still need to suppress.
        """
        now = utcnow()
        self.db.execute("DELETE FROM event_claims WHERE expires_at <= ?", (to_iso(now),))
        expires = now + (ttl or self.ttl)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO event_claims (key, claimed_at, expires_at)"
            " VALUES (?, ?, ?)",
            (key, to_iso(now), to_iso(expires)),
        )
        return cur.rowcount == 1

    def seen(self, key: str) -> bool:
        return self.db.one("SELECT 1 FROM event_claims WHERE key = ?", (key,)) is not None

    def release(self, key: str) -> None:
        """Drop a claim so the event can be reprocessed (used by replay)."""
        self.db.execute("DELETE FROM event_claims WHERE key = ?", (key,))

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM event_claims") or 0)
