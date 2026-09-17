"""Enrichment (design.md section 10)."""

from lep.enrich.bakeoff import BakeOffReport, bake_off
from lep.enrich.providers import (
    EnrichmentProvider,
    EnrichmentResult,
    StaticProvider,
    enrich_entity,
)
from lep.enrich.ttl import FIELD_TTL, expires_at, is_stale, reenrichment_candidates

__all__ = [
    "BakeOffReport",
    "EnrichmentProvider",
    "EnrichmentResult",
    "FIELD_TTL",
    "StaticProvider",
    "bake_off",
    "enrich_entity",
    "expires_at",
    "is_stale",
    "reenrichment_candidates",
]
