"""The conflict log (design.md section 8.3).

Log every conflict, *including* the auto-resolved ones.  The log is what tells
you whether the policy is right: a field generating constant conflicts usually
means the source-of-truth assignment is wrong, or that two teams are editing
the same thing in two systems -- an organisational problem the data surfaces.

It is free once observations are stored properly, because a conflict is not
something you detect: two live observations with different values *are* a
conflict by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Any

from lep.core.types import Observation, ResolvedValue, hash_value, utcnow
from lep.store.db import Database, dumps, from_iso, loads, to_iso


@dataclass
class Conflict:
    entity_id: str
    field: str
    candidates: list[Observation]
    resolved_to: Any
    resolution_reason: str
    detected_at: datetime = dc_field(default_factory=utcnow)
    reviewed: bool = False
    id: int | None = None
    #: Flattened snapshot of the competing observations, as stored.
    raw_candidates: list[dict[str, Any]] = dc_field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        if self.candidates:
            return sorted({o.source for o in self.candidates})
        return sorted({c["source"] for c in self.raw_candidates})


class ConflictLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, entity_id: str, field: str, resolved: ResolvedValue) -> Conflict | None:
        """Log ``resolved`` if its candidates disagreed.  Returns None if not."""
        if not resolved.is_conflicted:
            return None
        conflict = Conflict(
            entity_id=entity_id,
            field=field,
            candidates=list(resolved.candidates),
            resolved_to=resolved.value,
            resolution_reason=resolved.reason,
        )
        payload = [
            {
                "source": o.source,
                "source_kind": o.source_kind.value,
                "value": o.value,
                "value_hash": hash_value(o.value),
                "confidence": o.confidence,
                "observed_at": to_iso(o.observed_at),
            }
            for o in conflict.candidates
        ]
        cur = self.db.execute(
            """
            INSERT INTO conflicts
                (entity_id, field, candidates, resolved_to, resolution_reason,
                 detected_at, reviewed)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (
                entity_id,
                field,
                dumps(payload),
                dumps(resolved.value),
                resolved.reason,
                to_iso(conflict.detected_at),
            ),
        )
        conflict.id = int(cur.lastrowid)
        return conflict

    def top_conflicting_fields(self, limit: int = 10) -> list[tuple[str, int]]:
        """The most useful diagnostic the system produces.

        Surface this in the admin UI.  A field at the top of this list is
        usually a field whose declared master is wrong.
        """
        rows = self.db.query(
            "SELECT field, COUNT(*) AS n FROM conflicts GROUP BY field"
            " ORDER BY n DESC, field LIMIT ?",
            (limit,),
        )
        return [(r["field"], r["n"]) for r in rows]

    def for_entity(self, entity_id: str) -> list[Conflict]:
        rows = self.db.query(
            "SELECT * FROM conflicts WHERE entity_id = ? ORDER BY detected_at DESC",
            (entity_id,),
        )
        return [self._row(r) for r in rows]

    def unreviewed(self, limit: int = 50) -> list[Conflict]:
        rows = self.db.query(
            "SELECT * FROM conflicts WHERE reviewed = 0 ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        )
        return [self._row(r) for r in rows]

    def mark_reviewed(self, conflict_id: int) -> None:
        self.db.execute("UPDATE conflicts SET reviewed = 1 WHERE id = ?", (conflict_id,))

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM conflicts") or 0)

    def delete_for_entity(self, entity_id: str) -> int:
        cur = self.db.execute("DELETE FROM conflicts WHERE entity_id = ?", (entity_id,))
        return cur.rowcount

    def search(self, entity_id: str) -> list[Conflict]:
        return self.for_entity(entity_id)

    @property
    def name(self) -> str:
        return "conflicts"

    def _row(self, row: Any) -> Conflict:
        conflict = Conflict(
            id=row["id"],
            entity_id=row["entity_id"],
            field=row["field"],
            candidates=[],
            resolved_to=loads(row["resolved_to"]),
            resolution_reason=row["resolution_reason"],
            detected_at=from_iso(row["detected_at"]),
            reviewed=bool(row["reviewed"]),
        )
        # The stored payload is a flattened snapshot of the competing
        # observations: enough to explain the conflict without re-reading them.
        conflict.raw_candidates = loads(row["candidates"]) or []
        return conflict
