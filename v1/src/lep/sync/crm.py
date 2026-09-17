"""CRM client interface, and an in-memory fake that behaves like the real ones.

The fake is not a toy: it reproduces the behaviours the design document warns
about, because those are what the tests need to exercise.

* every write fires a webhook, so an unsuppressed sync loops;
* the modifier is recorded, but some changes attribute to nobody (formula
  fields, roll-up summaries, workflow rules) -- which is exactly when origin
  tagging fails and the write log has to catch it;
* ``system_modstamp`` advances on system-level changes that ``last_modified``
  misses, which is why polling reconciliation uses it (section 7.3);
* responses carry rate-limit headers, so the budget manager has something to
  read (section 3.3);
* a configurable fraction of webhooks is dropped, because every push mechanism
  drops events and the reconciliation backstop exists for that reason.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Protocol

from lep.core.types import CrmEvent, EntityType, SourceKind, utcnow


class CrmClient(Protocol):  # pragma: no cover - structural type
    name: str
    integration_user_id: str

    def read(self, record_id: str) -> dict[str, Any]: ...
    def write(self, record_id: str, field: str, value: Any, *, actor: str | None = None) -> None: ...
    def updated_since(self, since: datetime) -> list[tuple[str, dict[str, Any]]]: ...
    def headers(self) -> dict[str, str]: ...


@dataclass
class CrmRecord:
    record_id: str
    fields: dict[str, Any] = field(default_factory=dict)
    last_modified: datetime = field(default_factory=utcnow)
    #: Advances on system-level changes that ``last_modified`` misses.
    system_modstamp: datetime = field(default_factory=utcnow)
    last_modified_by: str | None = None
    deleted: bool = False
    object_type: EntityType = "person"


class FakeCrm:
    """In-memory CRM with webhooks, attribution quirks and rate-limit headers."""

    def __init__(
        self,
        name: str,
        *,
        integration_user_id: str = "integration-user",
        on_event: Callable[[CrmEvent], None] | None = None,
        drop_webhook_rate: float = 0.0,
        attribution_loss_rate: float = 0.0,
        daily_limit: int = 100_000,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.name = name
        self.integration_user_id = integration_user_id
        self.on_event = on_event
        self.drop_webhook_rate = drop_webhook_rate
        self.attribution_loss_rate = attribution_loss_rate
        self.daily_limit = daily_limit
        self.rng = rng or random.Random(0)
        #: Injectable so tests can run simulated days in milliseconds.
        self.clock = clock
        self.records: dict[str, CrmRecord] = {}
        self.calls = 0
        self.writes = 0
        self.dropped_webhooks = 0
        self._change_seq = 0

    # -------------------------------------------------------------- reads

    def read(self, record_id: str) -> dict[str, Any]:
        self.calls += 1
        record = self.records.get(record_id)
        return dict(record.fields) if record and not record.deleted else {}

    def read_many(self, record_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """One call for up to 200 ids -- ``WHERE Id IN (...)`` (section 3.3)."""
        self.calls += 1
        return {
            rid: dict(self.records[rid].fields)
            for rid in record_ids
            if rid in self.records and not self.records[rid].deleted
        }

    def updated_since(self, since: datetime) -> list[tuple[str, dict[str, Any]]]:
        """Polling on ``system_modstamp``, not ``last_modified`` (section 7.3)."""
        self.calls += 1
        return [
            (r.record_id, dict(r.fields))
            for r in self.records.values()
            if r.system_modstamp > since and not r.deleted
        ]

    def exists(self, record_id: str) -> bool:
        record = self.records.get(record_id)
        return bool(record and not record.deleted)

    # ------------------------------------------------------------- writes

    def write(
        self,
        record_id: str,
        field_name: str,
        value: Any,
        *,
        actor: str | None = None,
        source_kind: SourceKind = SourceKind.CRM,
        at: datetime | None = None,
    ) -> CrmEvent | None:
        """Write a field and fire the resulting webhook.

        ``actor`` of None models the attribution gaps: a workflow rule or a
        roll-up recalculation that the CRM attributes to nobody.
        """
        self.calls += 1
        self.writes += 1
        now = at or self.clock()
        record = self.records.setdefault(record_id, CrmRecord(record_id))
        if record.fields.get(field_name) == value:
            # No-op writes still cost a call but fire no webhook -- worth
            # modelling, because suppressing them is a real saving.
            return None
        record.fields[field_name] = value
        record.last_modified = now
        record.system_modstamp = now
        record.last_modified_by = actor
        self._change_seq += 1

        attributed = actor
        if attributed and self.rng.random() < self.attribution_loss_rate:
            attributed = None      # attribution lost: origin tagging will fail

        event = CrmEvent(
            source=self.name,
            record_id=record_id,
            field=field_name,
            new_value=value,
            observed_at=now,
            object_type=record.object_type,
            change_id=f"{self.name}-{self._change_seq}",
            last_modified_by_id=attributed if self.name == "salesforce" else None,
            property_source_id=attributed if self.name == "hubspot" else None,
            source_kind=source_kind,
            received_at=now,
        )
        self._emit(event)
        return event

    def user_edit(self, record_id: str, field_name: str, value: Any, user: str = "rep@acme.com"):
        """A human edit.  Never attributed to the integration user."""
        return self.write(record_id, field_name, value, actor=user, source_kind=SourceKind.USER)

    def system_touch(self, record_id: str, at: datetime | None = None) -> None:
        """A system-level change: ``system_modstamp`` moves, no webhook fires.

        This is the drift that polling reconciliation exists to find.
        """
        record = self.records.setdefault(record_id, CrmRecord(record_id))
        record.system_modstamp = at or self.clock()

    def delete(self, record_id: str) -> None:
        record = self.records.get(record_id)
        if record:
            record.deleted = True
            record.system_modstamp = self.clock()

    def _emit(self, event: CrmEvent) -> None:
        if self.on_event is None:
            return
        if self.rng.random() < self.drop_webhook_rate:
            # Every push mechanism drops events (section 7.3).
            self.dropped_webhooks += 1
            return
        self.on_event(event)

    # ------------------------------------------------------------ headers

    def headers(self) -> dict[str, str]:  # pragma: no cover - overridden
        return {}

    def seed(self, record_id: str, fields: dict[str, Any], *, object_type: EntityType = "person") -> CrmRecord:
        record = CrmRecord(record_id, dict(fields), object_type=object_type)
        self.records[record_id] = record
        return record
