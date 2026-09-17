"""Resolution: observations plus policy gives the current value.

The current value of a field is never stored (design.md section 4.1).  It is
computed here, which is what makes a policy change replayable against history
instead of a migration that destroys the old values.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from lep.conflict.strategies import FieldPolicy, PolicySet
from lep.core.types import ResolvedValue
from lep.store.observations import ObservationStore

if TYPE_CHECKING:  # pragma: no cover
    from lep.conflict.log import ConflictLog


def resolve(
    store: ObservationStore,
    entity_id: str,
    field: str,
    policy: FieldPolicy,
    *,
    at: datetime | None = None,
    as_of: datetime | None = None,
    conflict_log: "ConflictLog | None" = None,
) -> ResolvedValue:
    """Current value of ``field`` for ``entity_id``.

    ``at``
        evaluate expiry as of this instant (TTLs, section 10.3).
    ``as_of``
        only consider observations recorded by this instant -- "what did we
        believe last Tuesday".

    When a ``conflict_log`` is passed, every disagreement is recorded, whether
    or not the policy resolved it automatically (section 8.3).
    """
    observations = store.live(entity_id, field, at=at, as_of=as_of)
    resolved = policy.resolve(observations)
    if conflict_log is not None:
        conflict_log.record(entity_id, field, resolved)
    return resolved


def resolve_all(
    store: ObservationStore,
    entity_id: str,
    policies: PolicySet,
    *,
    object_type: str = "contact",
    at: datetime | None = None,
    as_of: datetime | None = None,
    conflict_log: "ConflictLog | None" = None,
) -> dict[str, ResolvedValue]:
    """Resolve every field this entity has observations for."""
    out: dict[str, ResolvedValue] = {}
    for field in store.fields_for(entity_id):
        out[field] = resolve(
            store,
            entity_id,
            field,
            policies.for_field(field, object_type),
            at=at,
            as_of=as_of,
            conflict_log=conflict_log,
        )
    return out


def current_record(
    store: ObservationStore,
    entity_id: str,
    policies: PolicySet,
    *,
    object_type: str = "contact",
    at: datetime | None = None,
) -> dict[str, object]:
    """Flat ``{field: value}`` view, for sync and for the UI."""
    return {
        field: rv.value
        for field, rv in resolve_all(
            store, entity_id, policies, object_type=object_type, at=at
        ).items()
        if rv.value is not None
    }
