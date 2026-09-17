"""Entities, external links, and merge records (design.md section 4.2).

The important property here is that a merge is a *recorded link*, not a
deletion.  ``canonical()`` walks the merge chain, so the merged entity's id
stays resolvable forever and its observations stay attached.  That is what
makes ``unmerge()`` possible in this store even though the CRM cannot do it --
and section 5.4 guarantees we will need it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lep.core.types import Entity, EntityLink, EntityType, Merge, new_id, utcnow
from lep.store.db import Database, from_iso, to_iso


class MergeCycleError(Exception):
    """Raised when a merge would create a cycle in the merge graph."""


class EntityStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ----------------------------------------------------------- entities

    def create(self, type: EntityType = "person", entity_id: str | None = None) -> Entity:
        entity = Entity(id=entity_id or new_id(), type=type, created_at=utcnow())
        self.db.execute(
            "INSERT OR IGNORE INTO entities (id, type, created_at) VALUES (?, ?, ?)",
            (entity.id, entity.type, to_iso(entity.created_at)),
        )
        return entity

    def get(self, entity_id: str) -> Entity | None:
        row = self.db.one("SELECT * FROM entities WHERE id = ?", (entity_id,))
        if row is None:
            return None
        return Entity(id=row["id"], type=row["type"], created_at=from_iso(row["created_at"]))

    def all_ids(self, type: EntityType | None = None) -> list[str]:
        if type is None:
            rows = self.db.query("SELECT id FROM entities")
        else:
            rows = self.db.query("SELECT id FROM entities WHERE type = ?", (type,))
        return [r["id"] for r in rows]

    def delete(self, entity_id: str) -> int:
        cur = self.db.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        return cur.rowcount

    # -------------------------------------------------------------- links

    def link(
        self,
        entity_id: str,
        source: str,
        source_record_id: str,
        *,
        confidence: float = 1.0,
        linked_by: str = "auto:v1",
    ) -> EntityLink:
        link = EntityLink(
            entity_id=entity_id,
            source=source,
            source_record_id=source_record_id,
            confidence=confidence,
            linked_at=utcnow(),
            linked_by=linked_by,
        )
        self.db.execute(
            """
            INSERT INTO entity_links
                (entity_id, source, source_record_id, confidence, linked_at, linked_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, source_record_id) DO UPDATE SET
                entity_id = excluded.entity_id,
                confidence = excluded.confidence,
                linked_at = excluded.linked_at,
                linked_by = excluded.linked_by
            """,
            (
                link.entity_id,
                link.source,
                link.source_record_id,
                link.confidence,
                to_iso(link.linked_at),
                link.linked_by,
            ),
        )
        return link

    def entity_for_record(self, source: str, source_record_id: str) -> str | None:
        row = self.db.one(
            "SELECT entity_id FROM entity_links WHERE source = ? AND source_record_id = ?",
            (source, source_record_id),
        )
        return self.canonical(row["entity_id"]) if row else None

    def links_for(self, entity_id: str, *, follow_merges: bool = True) -> list[EntityLink]:
        ids = [entity_id]
        if follow_merges:
            ids = sorted(self.merged_into(entity_id) | {entity_id})
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.query(
            f"SELECT * FROM entity_links WHERE entity_id IN ({placeholders})", ids
        )
        return [
            EntityLink(
                entity_id=r["entity_id"],
                source=r["source"],
                source_record_id=r["source_record_id"],
                confidence=r["confidence"],
                linked_at=from_iso(r["linked_at"]),
                linked_by=r["linked_by"],
            )
            for r in rows
        ]

    def unlink_entity(self, entity_id: str) -> int:
        cur = self.db.execute("DELETE FROM entity_links WHERE entity_id = ?", (entity_id,))
        return cur.rowcount

    # ------------------------------------------------------------- merges

    def merge(
        self,
        surviving_id: str,
        merged_id: str,
        *,
        score: float | None = None,
        decided_by: str = "auto:v1",
    ) -> Merge:
        """Record a merge and move observations onto the survivor.

        Nothing is deleted.  ``canonical(merged_id)`` keeps returning the
        survivor, and ``unmerge()`` puts it back.
        """
        surviving_id = self.canonical(surviving_id)
        merged_id = self.canonical(merged_id)
        if surviving_id == merged_id:
            raise MergeCycleError(f"{merged_id} is already merged into {surviving_id}")
        if surviving_id in self.merged_into(merged_id):
            raise MergeCycleError("merge would create a cycle")

        record = Merge(
            surviving_id=surviving_id,
            merged_id=merged_id,
            score=score,
            decided_by=decided_by,
            decided_at=utcnow(),
        )
        cur = self.db.execute(
            """
            INSERT INTO merges (surviving_id, merged_id, score, decided_by, decided_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.surviving_id,
                record.merged_id,
                record.score,
                record.decided_by,
                to_iso(record.decided_at),
            ),
        )
        merge_id = int(cur.lastrowid)
        # Links follow the survivor; observations keep their original entity_id
        # so that an unmerge is a pure metadata operation.
        self.db.execute(
            "UPDATE entity_links SET entity_id = ? WHERE entity_id = ?",
            (surviving_id, merged_id),
        )
        return Merge(**{**record.__dict__, "id": merge_id})

    def unmerge(self, merged_id: str, reason: str, *, by: str = "user") -> bool:
        """Revert the most recent active merge of ``merged_id``.

        Possible precisely because section 4.2 kept the observations attached to
        their original entity.
        """
        row = self.db.one(
            "SELECT * FROM merges WHERE merged_id = ? AND reverted_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (merged_id,),
        )
        if row is None:
            return False
        self.db.execute(
            "UPDATE merges SET reverted_at = ?, revert_reason = ? WHERE id = ?",
            (to_iso(utcnow()), f"{by}: {reason}", row["id"]),
        )
        # Put the external links back where their observations point.
        self.db.execute(
            """
            UPDATE entity_links SET entity_id = ?
            WHERE (source, source_record_id) IN (
                SELECT DISTINCT source, source_record_id FROM observations
                WHERE entity_id = ? AND source_record_id IS NOT NULL
            )
            """,
            (merged_id, merged_id),
        )
        return True

    def canonical(self, entity_id: str, *, at: datetime | None = None) -> str:
        """Follow the merge chain to the surviving entity."""
        seen = {entity_id}
        current = entity_id
        while True:
            sql = (
                "SELECT surviving_id FROM merges WHERE merged_id = ? AND reverted_at IS NULL"
            )
            params: list[Any] = [current]
            if at is not None:
                sql += " AND decided_at <= ?"
                params.append(to_iso(at))
            sql += " ORDER BY id DESC LIMIT 1"
            row = self.db.one(sql, params)
            if row is None:
                return current
            current = row["surviving_id"]
            if current in seen:  # pragma: no cover - guarded at write time
                raise MergeCycleError(f"cycle in merge chain at {current}")
            seen.add(current)

    def merged_into(self, surviving_id: str) -> set[str]:
        """All entity ids that resolve to ``surviving_id`` (transitively)."""
        out: set[str] = set()
        frontier = [surviving_id]
        while frontier:
            current = frontier.pop()
            rows = self.db.query(
                "SELECT merged_id FROM merges WHERE surviving_id = ? AND reverted_at IS NULL",
                (current,),
            )
            for row in rows:
                if row["merged_id"] not in out:
                    out.add(row["merged_id"])
                    frontier.append(row["merged_id"])
        return out

    def merges_for(self, entity_id: str) -> list[Merge]:
        rows = self.db.query(
            "SELECT * FROM merges WHERE surviving_id = ? OR merged_id = ? ORDER BY id",
            (entity_id, entity_id),
        )
        return [
            Merge(
                id=r["id"],
                surviving_id=r["surviving_id"],
                merged_id=r["merged_id"],
                score=r["score"],
                decided_by=r["decided_by"],
                decided_at=from_iso(r["decided_at"]),
                reverted_at=from_iso(r["reverted_at"]),
                revert_reason=r["revert_reason"],
            )
            for r in rows
        ]

    def delete_merges(self, entity_id: str) -> int:
        cur = self.db.execute(
            "DELETE FROM merges WHERE surviving_id = ? OR merged_id = ?",
            (entity_id, entity_id),
        )
        return cur.rowcount

    # Erasure support -------------------------------------------------------

    def search(self, entity_id: str) -> list[Any]:
        found: list[Any] = []
        if self.get(entity_id):
            found.append(entity_id)
        found.extend(self.links_for(entity_id, follow_merges=False))
        found.extend(self.merges_for(entity_id))
        return found

    @property
    def name(self) -> str:
        return "entities"
