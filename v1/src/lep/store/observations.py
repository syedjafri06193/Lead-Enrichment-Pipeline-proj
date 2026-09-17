"""The observation store (design.md section 4.1).

Append-only and bitemporal.  The current value of a field is never stored; it
is a pure function of these rows and a policy (:mod:`lep.store.resolve`).

The reason to pay for this: provenance becomes a query, policy changes become
replayable, conflicts exist by construction rather than being detected, and
erasure is provable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from lep.core.types import Observation, SourceKind, utcnow
from lep.store.db import Database, dumps, from_iso, loads, to_iso


def _row_to_obs(row: Any) -> Observation:
    return Observation(
        id=row["id"],
        entity_id=row["entity_id"],
        field=row["field"],
        value=loads(row["value"]),
        source=row["source"],
        source_kind=SourceKind(row["source_kind"]),
        source_record_id=row["source_record_id"],
        confidence=row["confidence"],
        observed_at=from_iso(row["observed_at"]),
        recorded_at=from_iso(row["recorded_at"]),
        expires_at=from_iso(row["expires_at"]),
        superseded_by=row["superseded_by"],
    )


class ObservationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ write

    def append(self, obs: Observation) -> Observation:
        """Record an observation.  Never overwrites anything."""
        cur = self.db.execute(
            """
            INSERT INTO observations
                (entity_id, field, value, source, source_kind, source_record_id,
                 confidence, observed_at, recorded_at, expires_at, superseded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.entity_id,
                obs.field,
                dumps(obs.value),
                obs.source,
                obs.source_kind.value,
                obs.source_record_id,
                obs.confidence,
                to_iso(obs.observed_at),
                to_iso(obs.recorded_at),
                to_iso(obs.expires_at),
                obs.superseded_by,
            ),
        )
        return obs.with_id(int(cur.lastrowid))

    def append_many(self, batch: Iterable[Observation]) -> list[Observation]:
        return [self.append(o) for o in batch]

    def record_superseded(self, obs: Observation, by_id: int | None = None) -> Observation:
        """Store an out-of-order observation without letting it win.

        Section 9.2: keep it, don't apply it.  It is evidence, it is needed for
        replay after a policy change, and it may become the winner under a
        different policy.
        """
        if by_id is None:
            latest = self.latest(obs.entity_id, obs.field, source=obs.source)
            by_id = latest.id if latest else None
        stored = self.append(obs)
        self.db.execute(
            "UPDATE observations SET superseded_by = ? WHERE id = ?",
            (by_id, stored.id),
        )
        return Observation(**{**stored.__dict__, "superseded_by": by_id})

    def reassign_entity(self, from_entity: str, to_entity: str) -> int:
        """Point one entity's observations at another (used by merge).

        The merge record in :mod:`lep.store.entities` is what makes this
        reversible; see section 4.2.
        """
        cur = self.db.execute(
            "UPDATE observations SET entity_id = ? WHERE entity_id = ?",
            (to_entity, from_entity),
        )
        return cur.rowcount

    # ------------------------------------------------------------------- read

    def observations(
        self,
        entity_id: str,
        field: str | None = None,
        *,
        as_of: datetime | None = None,
        include_superseded: bool = True,
    ) -> list[Observation]:
        """All observations for an entity, optionally as of a point in time.

        ``as_of`` filters on ``recorded_at`` -- "what did we know then" -- which
        is the question that makes an audit answerable.
        """
        sql = ["SELECT * FROM observations WHERE entity_id = ?"]
        params: list[Any] = [entity_id]
        if field is not None:
            sql.append("AND field = ?")
            params.append(field)
        if as_of is not None:
            sql.append("AND recorded_at <= ?")
            params.append(to_iso(as_of))
        if not include_superseded:
            sql.append("AND superseded_by IS NULL")
        sql.append("ORDER BY observed_at DESC, id DESC")
        return [_row_to_obs(r) for r in self.db.query(" ".join(sql), params)]

    def live(
        self,
        entity_id: str,
        field: str,
        *,
        at: datetime | None = None,
        as_of: datetime | None = None,
    ) -> list[Observation]:
        """Observations eligible for resolution: not expired at ``at``."""
        at = at or utcnow()
        return [
            o
            for o in self.observations(entity_id, field, as_of=as_of)
            if not o.is_expired(at)
        ]

    def latest(
        self, entity_id: str, field: str, *, source: str | None = None
    ) -> Observation | None:
        """Most recent observation by ``observed_at``.

        Restricted to one source when given, because comparing ``observed_at``
        across sources means comparing clocks we do not control (section 9.2).
        """
        sql = "SELECT * FROM observations WHERE entity_id = ? AND field = ?"
        params: list[Any] = [entity_id, field]
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        sql += " AND superseded_by IS NULL ORDER BY observed_at DESC, id DESC LIMIT 1"
        row = self.db.one(sql, params)
        return _row_to_obs(row) if row else None

    def fields_for(self, entity_id: str) -> list[str]:
        rows = self.db.query(
            "SELECT DISTINCT field FROM observations WHERE entity_id = ? ORDER BY field",
            (entity_id,),
        )
        return [r["field"] for r in rows]

    def entity_ids(self) -> list[str]:
        rows = self.db.query("SELECT DISTINCT entity_id FROM observations")
        return [r["entity_id"] for r in rows]

    def by_source_record(self, source: str, source_record_id: str) -> list[Observation]:
        rows = self.db.query(
            "SELECT * FROM observations WHERE source = ? AND source_record_id = ?"
            " ORDER BY observed_at DESC",
            (source, source_record_id),
        )
        return [_row_to_obs(r) for r in rows]

    def count(self, entity_id: str | None = None) -> int:
        if entity_id is None:
            return int(self.db.scalar("SELECT COUNT(*) FROM observations") or 0)
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM observations WHERE entity_id = ?", (entity_id,)
            )
            or 0
        )

    # ---------------------------------------------------------------- erasure

    def delete_for_entity(self, entity_id: str) -> int:
        """Hard delete.  Only ever called by :mod:`lep.privacy.erasure`."""
        cur = self.db.execute(
            "DELETE FROM observations WHERE entity_id = ?", (entity_id,)
        )
        return cur.rowcount

    def search(self, entity_id: str) -> list[Observation]:
        """Used by the erasure completeness test (section 17.1)."""
        return self.observations(entity_id)

    @property
    def name(self) -> str:
        return "observations"
