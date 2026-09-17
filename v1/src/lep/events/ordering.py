"""Out-of-order arrival (design.md section 9.2).

Webhooks arrive out of order routinely, and an update can precede the create it
depends on.

Two rules:

* Compare ``observed_at`` **within a source** -- one clock.  Across sources,
  ordering is a policy question, not a timestamp question (section 8.2).
* **Store out-of-order observations rather than discarding them.**  They are
  evidence, they are needed for replay after a policy change, and a
  late-arriving observation may become the winner under a different policy.

The consequence worth stating: replaying the same event stream in any order
must produce the same resolved state.  That is the M3 exit criterion and it is
what :func:`apply_observation` is arranged to guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lep.core.types import Observation
from lep.store.observations import ObservationStore


class OrderingResult(str, Enum):
    APPLIED = "applied"
    SUPERSEDED = "superseded"   # kept, but it does not win
    DUPLICATE = "duplicate"     # byte-identical to one we already hold


@dataclass(frozen=True)
class ApplyOutcome:
    result: OrderingResult
    observation: Observation


def apply_observation(store: ObservationStore, obs: Observation) -> ApplyOutcome:
    """Store ``obs``, marking it superseded if it is older news.

    Nothing is ever dropped.  "Superseded" is a flag on a stored row, not a
    deletion, so a later policy change can still see it.
    """
    latest = store.latest(obs.entity_id, obs.field, source=obs.source)

    if latest is not None and latest.observed_at == obs.observed_at:
        if latest.value_hash == obs.value_hash:
            # Same source, same instant, same value: a redelivery that got past
            # the idempotency check (different change id, identical content).
            return ApplyOutcome(OrderingResult.DUPLICATE, latest)

    if latest is not None and obs.observed_at <= latest.observed_at:
        stored = store.record_superseded(obs, latest.id)
        return ApplyOutcome(OrderingResult.SUPERSEDED, stored)

    return ApplyOutcome(OrderingResult.APPLIED, store.append(obs))


def provisional_entity(entity_store, source: str, record_id: str, type: str = "person") -> str:
    """Create-after-update: make a provisional entity and let the create fill it.

    Discarding an orphan update loses data that will not be redelivered.
    """
    existing = entity_store.entity_for_record(source, record_id)
    if existing:
        return existing
    entity = entity_store.create(type)
    entity_store.link(
        entity.id, source, record_id, confidence=1.0, linked_by="auto:provisional"
    )
    return entity.id
