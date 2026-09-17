"""Erasure and provenance (design.md sections 11.2, 11.3).

A deletion request has to reach observations, entity links and merge records,
cached enrichment payloads, search and matching indexes, derived clusters,
event logs and DLQ contents, backups (or a documented backup-expiry policy),
and the CRMs we sync to.

Two things are easy to get wrong:

* **The suppression record.**  Without it the next backfill or re-enrichment
  resurrects the person -- a failure mode that is both common and exactly what
  the regulation is about.
* **The report.**  "We deleted it" is a claim; the report is the evidence, and
  a regulator can ask for evidence.

Provenance is the other half.  "Where did this data come from?" is a question a
regulator can ask, and the observation store answers it directly -- the
bitemporal store is not just good engineering, it is the artifact that makes a
subject access request answerable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Protocol

from lep.core.types import utcnow
from lep.store.db import Database, dumps, to_iso


class ErasableStore(Protocol):  # pragma: no cover - structural type
    name: str

    def search(self, entity_id: str) -> list[Any]: ...
    def delete_for_entity(self, entity_id: str) -> int: ...


@dataclass
class ErasureReport:
    entity_id: str
    reason: str
    erased_at: datetime = field(default_factory=utcnow)
    deleted: dict[str, int] = field(default_factory=dict)
    crm_requests: list[dict[str, str]] = field(default_factory=list)
    suppression_keys: list[str] = field(default_factory=list)
    residue: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not any(self.residue.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "reason": self.reason,
            "erased_at": to_iso(self.erased_at),
            "deleted": self.deleted,
            "crm_requests": self.crm_requests,
            "suppression_keys": self.suppression_keys,
            "residue": self.residue,
            "complete": self.complete,
        }

    def render(self) -> str:
        lines = [f"Erasure of {self.entity_id} ({self.reason}) at {self.erased_at.isoformat()}"]
        for store, count in sorted(self.deleted.items()):
            lines.append(f"  {store:<16} {count} row(s) removed")
        for request in self.crm_requests:
            lines.append(f"  crm request     {request['source']}:{request['record_id']}")
        lines.append(f"  suppression     {', '.join(self.suppression_keys) or 'none'}")
        lines.append(f"  complete        {self.complete}")
        return "\n".join(lines)


class SuppressionList:
    """Stops an erased person being re-imported or re-enriched.

    Keyed by entity id *and* by any stable identifier we held (normalized
    email, phone), because the next backfill will arrive with the identifier,
    not with our internal id.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, key: str, reason: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO erasure_suppression (key, reason, created_at)"
            " VALUES (?, ?, ?)",
            (key, reason, to_iso(utcnow())),
        )

    def contains(self, key: str) -> bool:
        return (
            self.db.one("SELECT 1 FROM erasure_suppression WHERE key = ?", (key,))
            is not None
        )

    def keys(self) -> list[str]:
        return [r["key"] for r in self.db.query("SELECT key FROM erasure_suppression")]

    def filter(self, candidates: Iterable[str]) -> list[str]:
        """Drop anything suppressed.  Call this at the top of every import."""
        return [c for c in candidates if not self.contains(c)]


def erase(
    entity_id: str,
    reason: str,
    *,
    db: Database,
    stores: Iterable[ErasableStore],
    crm_clients: dict[str, Any] | None = None,
    entity_store: Any | None = None,
    identifiers: Iterable[str] = (),
    verify: bool = True,
) -> ErasureReport:
    """Delete a person from every store, and prove it.

    The ``verify`` pass re-searches every store afterwards and records what it
    found.  A report claiming completeness that nobody checked is worth nothing
    -- and the test in ``tests/test_erasure_complete.py`` checks exactly this.
    """
    report = ErasureReport(entity_id=entity_id, reason=reason)
    suppression = SuppressionList(db)

    # Ask the CRMs before we forget which records were theirs.
    links = entity_store.links_for(entity_id) if entity_store else []
    for link in links:
        report.crm_requests.append(
            {"source": link.source, "record_id": link.source_record_id}
        )
        client = (crm_clients or {}).get(link.source)
        if client is not None and hasattr(client, "delete"):
            client.delete(link.source_record_id)

    for store in stores:
        report.deleted[store.name] = store.delete_for_entity(entity_id)

    if entity_store is not None:
        report.deleted["entity_links"] = entity_store.unlink_entity(entity_id)
        report.deleted["merges"] = entity_store.delete_merges(entity_id)
        report.deleted["entities"] = entity_store.delete(entity_id)

    # Suppression, so the next import does not undo all of the above.
    keys = [entity_id, *identifiers, *(f"{l.source}:{l.source_record_id}" for l in links)]
    for key in keys:
        suppression.add(key, reason)
    report.suppression_keys = keys

    if verify:
        for store in stores:
            found = store.search(entity_id)
            if found:
                report.residue[store.name] = len(found)
        if entity_store is not None:
            found = entity_store.search(entity_id)
            if found:
                report.residue["entities"] = len(found)

    db.execute(
        "INSERT INTO erasure_log (entity_id, reason, report, erased_at) VALUES (?, ?, ?, ?)",
        (entity_id, reason, dumps(report.to_dict()), to_iso(report.erased_at)),
    )
    return report


def provenance(observation_store, entity_id: str, field_name: str | None = None) -> list[dict[str, Any]]:
    """Answer "where did this come from?" for a DSAR.

    A query, not an investigation (section 4.1).
    """
    observations = (
        observation_store.observations(entity_id, field_name)
        if field_name
        else observation_store.observations(entity_id)
    )
    return [
        {
            "field": o.field,
            "value": o.value,
            "source": o.source,
            "source_kind": o.source_kind.value,
            "source_record_id": o.source_record_id,
            "confidence": o.confidence,
            "observed_at": to_iso(o.observed_at),
            "recorded_at": to_iso(o.recorded_at),
            "expired": o.is_expired(),
        }
        for o in observations
    ]
