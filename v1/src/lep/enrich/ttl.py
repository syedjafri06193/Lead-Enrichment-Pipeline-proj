"""Staleness (design.md section 10.3).

B2B contact data decays substantially year over year, mostly from job changes.
Enrichment has a shelf life, so every enrichment observation gets an
``expires_at``.

Expired observations are **excluded from resolution but not deleted** -- they
are still history, and they are evidence that a value changed.

Re-enrichment is a budget decision, not a schedule: re-enriching everything
quarterly is expensive, re-enriching records somebody is actually using is
targeted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Sequence

from lep.core.types import Observation, utcnow

FIELD_TTL: dict[str, timedelta | None] = {
    "job_title": timedelta(days=90),
    "company": timedelta(days=90),
    "normalized_company": timedelta(days=90),
    "email": timedelta(days=180),
    "phone": timedelta(days=180),
    "employee_count": timedelta(days=180),
    "industry": timedelta(days=365),
    "founded_year": None,        # immutable
    "first_name": None,
    "last_name": None,
}


def ttl_for(field: str, overrides: dict[str, timedelta | None] | None = None) -> timedelta | None:
    if overrides and field in overrides:
        return overrides[field]
    return FIELD_TTL.get(field)


def expires_at(
    field: str,
    observed_at: datetime | None = None,
    overrides: dict[str, timedelta | None] | None = None,
) -> datetime | None:
    ttl = ttl_for(field, overrides)
    if ttl is None:
        return None
    return (observed_at or utcnow()) + ttl


def is_stale(obs: Observation, at: datetime | None = None) -> bool:
    return obs.is_expired(at)


def reenrichment_candidates(
    entities: Iterable[str],
    last_enriched: dict[str, datetime],
    engagement: dict[str, int],
    *,
    at: datetime | None = None,
    field: str = "job_title",
    budget: int = 100,
    min_engagement: int = 1,
) -> list[str]:
    """Which records are worth spending enrichment credits on.

    Ordered by engagement, not by age: prioritise by whether anyone is actually
    using the record.  A stale record nobody touches costs nothing to leave
    stale; a stale record a rep is emailing today is the expensive one.
    """
    now = at or utcnow()
    ttl = ttl_for(field) or timedelta(days=365)
    stale = [
        entity
        for entity in entities
        if engagement.get(entity, 0) >= min_engagement
        and now - last_enriched.get(entity, datetime.min.replace(tzinfo=now.tzinfo)) >= ttl
    ]
    stale.sort(key=lambda e: (-engagement.get(e, 0), last_enriched.get(e, now)))
    return stale[:budget]


def apply_ttls(observations: Sequence[Observation]) -> list[Observation]:
    """Stamp ``expires_at`` on observations that do not have one."""
    out = []
    for obs in observations:
        if obs.expires_at is not None:
            out.append(obs)
            continue
        out.append(
            Observation(**{**obs.__dict__, "expires_at": expires_at(obs.field, obs.observed_at)})
        )
    return out
