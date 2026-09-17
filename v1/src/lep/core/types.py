"""Core value types.

Nothing in here talks to a database.  The store layer persists these; the
resolution, matching and sync layers pass them around.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

EntityType = Literal["person", "company"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def hash_value(value: Any) -> str:
    """Stable hash of an observation value.

    Used by the sync write log (design.md section 7.2) where we only ever need
    equality, and storing the value itself would make the log enormous.
    """
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class SourceKind(str, Enum):
    """What kind of actor produced an observation.

    ``USER`` is privileged: ``HUMAN_WINS`` (design.md section 8.2) exists so a
    person's correction is never overwritten by an automated one.
    """

    USER = "user"
    CRM = "crm"
    ENRICHMENT = "enrichment"
    SYSTEM = "system"


class Decision(str, Enum):
    """Outcome of a match decision (design.md section 5.4)."""

    AUTO_MERGE = "auto_merge"
    REVIEW = "review"
    DISTINCT = "distinct"


@dataclass(frozen=True)
class Observation:
    """One thing some source claimed about one field of one entity.

    Bitemporal by construction (design.md section 4.1):

    ``observed_at``
        the source system's notion of when the value was true.
    ``recorded_at``
        when we learned it.

    They diverge whenever a webhook is delayed or a backfill imports history,
    and conflating them makes ordering wrong.
    """

    entity_id: str
    field: str
    value: Any
    source: str
    observed_at: datetime
    recorded_at: datetime = field(default_factory=utcnow)
    source_kind: SourceKind = SourceKind.CRM
    source_record_id: str | None = None
    confidence: float = 1.0
    expires_at: datetime | None = None
    id: int | None = None
    superseded_by: int | None = None

    def is_expired(self, at: datetime | None = None) -> bool:
        """Expired observations are excluded from resolution, never deleted.

        They are still history, and still evidence that a value changed
        (design.md section 10.3).
        """
        if self.expires_at is None:
            return False
        return (at or utcnow()) >= self.expires_at

    @property
    def value_hash(self) -> str:
        return hash_value(self.value)

    def with_id(self, obs_id: int) -> Observation:
        return Observation(**{**self.__dict__, "id": obs_id})


@dataclass(frozen=True)
class ResolvedValue:
    """The current value of a field, plus why it is that value.

    ``reason`` and ``winning_observation`` are what make provenance a query
    rather than an investigation (design.md section 4.1).
    """

    value: Any
    reason: str
    source: str | None = None
    observed_at: datetime | None = None
    winning_observation: Observation | None = None
    candidates: tuple[Observation, ...] = ()
    #: True when the policy could not decide safely on its own and a human
    #: should look at it (MANUAL, or cross-source ambiguity under MOST_RECENT).
    needs_review: bool = False

    @property
    def is_conflicted(self) -> bool:
        """True when live observations disagreed, whether or not we resolved it."""
        distinct = {hash_value(o.value) for o in self.candidates}
        return len(distinct) > 1


@dataclass(frozen=True)
class Entity:
    id: str
    type: EntityType
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class EntityLink:
    """Which external record is believed to be this entity."""

    entity_id: str
    source: str
    source_record_id: str
    confidence: float
    linked_at: datetime = field(default_factory=utcnow)
    linked_by: str = "auto:v1"


@dataclass(frozen=True)
class Merge:
    """A recorded merge.

    Merges are links, never deletions (design.md section 4.2).  The merged
    entity's id stays resolvable and its observations stay attached, which is
    what makes unmerge possible here even though it is not in the CRM.
    """

    surviving_id: str
    merged_id: str
    score: float | None
    decided_by: str
    decided_at: datetime = field(default_factory=utcnow)
    reverted_at: datetime | None = None
    revert_reason: str | None = None
    id: int | None = None

    @property
    def is_active(self) -> bool:
        return self.reverted_at is None


@dataclass(frozen=True)
class CrmEvent:
    """A change arriving from a CRM.

    ``change_id`` is the provider's stable identifier for the change where one
    exists; ``idempotency_key`` falls back to a payload hash when it does not
    (design.md section 9.1).
    """

    source: str
    record_id: str
    field: str
    new_value: Any
    observed_at: datetime
    object_type: EntityType = "person"
    change_id: str | None = None
    last_modified_by_id: str | None = None
    property_source_id: str | None = None
    received_at: datetime = field(default_factory=utcnow)
    source_kind: SourceKind = SourceKind.CRM
    attempt: int = 0

    @property
    def idempotency_key(self) -> str:
        if self.change_id:
            return f"{self.source}:{self.record_id}:{self.change_id}"
        # No stable change id.  Hash the meaningful payload only -- delivery
        # timestamps and attempt counters vary between redeliveries, and
        # including them would make every redelivery look new.
        digest = hash_value(
            {
                "source": self.source,
                "record_id": self.record_id,
                "field": self.field,
                "value": self.new_value,
                "observed_at": self.observed_at.isoformat(),
            }
        )
        return f"{self.source}:{self.record_id}:{digest}"


@dataclass
class Record:
    """A flat external record, as read from a CRM or a file.

    This is the input to matching.  ``fields`` holds raw source values;
    ``normalized`` is filled in by :mod:`lep.identity.normalize`.
    """

    source: str
    source_record_id: str
    fields: dict[str, Any] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)
    entity_id: str | None = None
    type: EntityType = "person"

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_record_id}"

    def get(self, name: str, default: Any = None) -> Any:
        if name in self.normalized:
            return self.normalized[name]
        return self.fields.get(name, default)


DEFAULT_ECHO_WINDOW = timedelta(minutes=5)
