"""The sync engine (design.md sections 7, 8, 9, 12.1).

Inbound: a CRM change becomes an observation, after the circuit breaker and
both echo defenses have had a look at it.

Outbound: the *resolved* value of a field -- not the inbound value -- is written
to whichever systems the field policy says may receive it, and every write is
recorded in the write log so its echo can be recognised on the way back.

The asymmetry worth noticing: inbound is cheap and lossless (record everything,
resolve later), outbound is expensive and destructive (it overwrites a
customer's data and spends their API budget).  So outbound is where the
gatekeeping is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from lep.budget.manager import BudgetManager, Priority
from lep.conflict.log import ConflictLog
from lep.conflict.strategies import PolicySet
from lep.core.types import (
    DEFAULT_ECHO_WINDOW,
    CrmEvent,
    Observation,
    SourceKind,
    hash_value,
    utcnow,
)
from lep.events.ordering import OrderingResult, apply_observation, provisional_entity
from lep.store.entities import EntityStore
from lep.store.observations import ObservationStore
from lep.store.resolve import resolve
from lep.sync.circuit import LoopDetector
from lep.sync.crm import FakeCrm
from lep.sync.echo import EchoSuppressor, WriteLog

SALESFORCE_MAPPING = {
    "email": "Email",
    "first_name": "FirstName",
    "last_name": "LastName",
    "phone": "Phone",
    "job_title": "Title",
    "owner_id": "OwnerId",
}

HUBSPOT_MAPPING = {
    "email": "email",
    "first_name": "firstname",
    "last_name": "lastname",
    "phone": "phone",
    "job_title": "jobtitle",
    "lifecycle_stage": "lifecyclestage",
}


@dataclass
class CrmMapping:
    """Canonical field names to CRM field names, versioned (section 12.1)."""

    source: str
    to_crm: dict[str, str]
    version: str = "v1"

    @property
    def from_crm(self) -> dict[str, str]:
        return {v: k for k, v in self.to_crm.items()}

    def canonical(self, crm_field: str) -> str | None:
        return self.from_crm.get(crm_field)

    def remote(self, field: str) -> str | None:
        return self.to_crm.get(field)


DEFAULT_MAPPINGS = {
    "salesforce": CrmMapping("salesforce", SALESFORCE_MAPPING),
    "hubspot": CrmMapping("hubspot", HUBSPOT_MAPPING),
}


@dataclass
class SyncStats:
    events_in: int = 0
    echoes_suppressed: int = 0
    quarantined: int = 0
    observations: int = 0
    superseded: int = 0
    duplicates: int = 0
    writes: int = 0
    no_change_skips: int = 0
    budget_denied: int = 0
    unmapped: int = 0
    user_edits: int = 0
    deletes_to_review: int = 0

    def summary(self) -> str:
        return (
            f"in={self.events_in} echo_suppressed={self.echoes_suppressed} "
            f"obs={self.observations} writes={self.writes} "
            f"skipped={self.no_change_skips} denied={self.budget_denied}"
        )


class SyncEngine:
    """Bidirectional sync with echo suppression and field-level policy."""

    def __init__(
        self,
        *,
        observations: ObservationStore,
        entities: EntityStore,
        policies: PolicySet,
        clients: dict[str, FakeCrm],
        write_log: WriteLog,
        loop_detector: LoopDetector,
        budget: BudgetManager | None = None,
        conflict_log: ConflictLog | None = None,
        mappings: dict[str, CrmMapping] | None = None,
        echo_window: timedelta = DEFAULT_ECHO_WINDOW,
        review_queue: Any | None = None,
        create_missing_records: bool = True,
        object_type: str = "contact",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.observations = observations
        self.entities = entities
        self.policies = policies
        self.clients = clients
        self.write_log = write_log
        self.loop_detector = loop_detector
        self.budget = budget
        self.conflict_log = conflict_log
        self.mappings = mappings or dict(DEFAULT_MAPPINGS)
        self.review_queue = review_queue
        self.create_missing_records = create_missing_records
        self.object_type = object_type
        self.clock = clock
        self.stats = SyncStats()
        self.echo = EchoSuppressor(
            write_log,
            {name: client.integration_user_id for name, client in clients.items()},
            window=echo_window,
        )
        #: Projection of what we believe each remote record holds, so a push
        #: that would be a no-op costs nothing.  Rebuilt by reconciliation.
        self.remote_state: dict[tuple[str, str], dict[str, Any]] = {}
        self._dirty: set[tuple[str, str]] = set()
        self._auto_record_seq = 0

    # ------------------------------------------------------------- inbound

    def ingest(self, event: CrmEvent) -> bool:
        """Turn an inbound change into an observation.  True if it was kept."""
        self.stats.events_in += 1
        if event.source_kind is SourceKind.USER:
            self.stats.user_edits += 1

        self.loop_detector.observe(event.source, event.record_id, event.field)
        if not self.loop_detector.check(event.source, event.record_id):
            self.stats.quarantined += 1
            return False

        if self.echo.is_echo(event):
            self.stats.echoes_suppressed += 1
            # Still update the projection: the remote really does hold this.
            self._remember_remote(event.source, event.record_id, event.field, event.new_value)
            return False

        mapping = self.mappings.get(event.source)
        canonical = mapping.canonical(event.field) if mapping else event.field
        if canonical is None:
            # An unmapped field is not an error, but it is worth counting: a
            # spike here usually means a CRM admin renamed something.
            self.stats.unmapped += 1
            return False

        entity_id = provisional_entity(
            self.entities, event.source, event.record_id, event.object_type
        )
        policy = self.policies.for_field(canonical, self.object_type)
        expires_at = None
        if policy.ttl is not None:
            expires_at = event.observed_at + policy.ttl

        observation = Observation(
            entity_id=entity_id,
            field=canonical,
            value=event.new_value,
            source=event.source,
            source_kind=event.source_kind,
            source_record_id=event.record_id,
            observed_at=event.observed_at,
            recorded_at=event.received_at,
            expires_at=expires_at,
        )
        outcome = apply_observation(self.observations, observation)
        if outcome.result is OrderingResult.APPLIED:
            self.stats.observations += 1
        elif outcome.result is OrderingResult.SUPERSEDED:
            self.stats.superseded += 1
        else:
            self.stats.duplicates += 1

        self._remember_remote(event.source, event.record_id, event.field, event.new_value)
        self._dirty.add((entity_id, canonical))
        return outcome.result is not OrderingResult.DUPLICATE

    def ingest_delete(self, source: str, record_id: str) -> None:
        """Deletes go to review; they are never propagated automatically.

        Section 8.4: an unwanted propagated delete destroys data across several
        systems at once, and the asymmetry is the same as for merges.
        """
        self.stats.deletes_to_review += 1
        if self.review_queue is not None:
            self.review_queue.enqueue_delete(
                source, record_id, "record deleted in source; propagation needs a human"
            )

    # ------------------------------------------------------------ outbound

    def push(
        self,
        entity_id: str,
        field_name: str,
        *,
        priority: Priority = Priority.INCREMENTAL,
        at: datetime | None = None,
    ) -> int:
        """Write the resolved value of one field to every eligible target."""
        policy = self.policies.for_field(field_name, self.object_type)
        resolved = resolve(
            self.observations,
            entity_id,
            field_name,
            policy,
            at=at or self.clock(),
            conflict_log=self.conflict_log,
        )
        if resolved.value is None:
            return 0
        if resolved.needs_review:
            # The policy could not decide safely.  Writing anyway would launder
            # an unresolved conflict into both systems.
            if self.review_queue is not None:
                self.review_queue.enqueue(
                    "pair",
                    entity_id,
                    reason=f"unresolved conflict on {field_name}: {resolved.reason}",
                    payload={"field": field_name, "value": resolved.value},
                    priority=0.8,
                )
            return 0

        written = 0
        for target, client in self.clients.items():
            if not policy.writes_to(target):
                continue
            record_id = self._record_id_for(entity_id, target)
            if record_id is None:
                continue
            mapping = self.mappings.get(target)
            remote_field = mapping.remote(field_name) if mapping else field_name
            if remote_field is None:
                continue
            if self.loop_detector.is_quarantined(target, record_id):
                continue

            current = self.remote_state.get((target, record_id), {}).get(remote_field, _MISSING)
            if current is not _MISSING and hash_value(current) == hash_value(resolved.value):
                self.stats.no_change_skips += 1
                continue

            if self.budget is not None and not self.budget.reserve(
                target, 1, priority, note=f"sync:{field_name}"
            ):
                self.stats.budget_denied += 1
                continue

            # Record the write BEFORE making it: the webhook can arrive before
            # the call returns, and an echo we have not logged yet is an echo we
            # will not recognise.
            self.write_log.record(target, record_id, remote_field, resolved.value)
            client.write(
                record_id,
                remote_field,
                resolved.value,
                actor=client.integration_user_id,
                source_kind=SourceKind.SYSTEM,
            )
            if self.budget is not None:
                self.budget.observe_headers(target, client.headers())
            self._remember_remote(target, record_id, remote_field, resolved.value)
            self.stats.writes += 1
            written += 1
        return written

    def flush(self, *, priority: Priority = Priority.INCREMENTAL) -> int:
        """Push everything that changed since the last flush."""
        dirty, self._dirty = self._dirty, set()
        return sum(self.push(entity_id, field, priority=priority) for entity_id, field in dirty)

    def run_cycle(self, events: Iterable[CrmEvent] = ()) -> int:
        for event in events:
            self.ingest(event)
        return self.flush()

    # -------------------------------------------------------------- helpers

    def _record_id_for(self, entity_id: str, target: str) -> str | None:
        for link in self.entities.links_for(entity_id):
            if link.source == target:
                return link.source_record_id
        if not self.create_missing_records:
            return None
        self._auto_record_seq += 1
        record_id = f"{target}-auto-{self._auto_record_seq}"
        self.entities.link(
            entity_id, target, record_id, confidence=1.0, linked_by="auto:sync"
        )
        return record_id

    def _remember_remote(self, source: str, record_id: str, field_name: str, value: Any) -> None:
        self.remote_state.setdefault((source, record_id), {})[field_name] = value

    # Convenience accessors used by the soak test (section 17.1).

    def total_writes(self) -> int:
        return self.stats.writes

    def total_user_edits(self) -> int:
        return self.stats.user_edits

    def loop_breaker_trips(self) -> int:
        return self.loop_detector.trips


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()
