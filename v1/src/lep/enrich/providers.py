"""Enrichment provider adapters (design.md section 10).

Two rules the adapters exist to enforce:

* enrichment writes **observations**, never values, so a provider can never
  overwrite anything -- it can only add evidence, which the field policy then
  weighs (section 4.1);
* every enrichment observation carries a TTL and a confidence, so a stale or
  low-confidence claim loses to a fresh or human one by construction.

And the rule that matters most in practice (section 10.4): *never let
enrichment overwrite a human*.  That is enforced by ``HUMAN_WINS`` being the
default strategy for human-editable fields, and it is the single fastest way to
destroy trust if you get it wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Protocol, Sequence

from lep.core.types import Observation, Record, SourceKind, utcnow
from lep.enrich.ttl import expires_at
from lep.store.observations import ObservationStore


@dataclass
class EnrichmentResult:
    """What one provider returned for one record."""

    provider: str
    fields: dict[str, Any] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    cost_credits: float = 0.0
    observed_at: datetime = field(default_factory=utcnow)
    #: A provider returning *something* is not a provider returning something
    #: correct.  Coverage claims mean less than they sound (section 10.1).
    matched: bool = True


class EnrichmentProvider(Protocol):  # pragma: no cover - structural type
    name: str
    cost_per_call: float

    def enrich(self, record: Record) -> EnrichmentResult: ...


class StaticProvider:
    """Provider backed by a fixed table.  Used for tests and the bake-off demo."""

    def __init__(
        self,
        name: str,
        data: dict[str, dict[str, Any]],
        *,
        cost_per_call: float = 1.0,
        confidence: float | dict[str, float] = 0.8,
        coverage_key: Callable[[Record], str | None] | None = None,
    ) -> None:
        self.name = name
        self.data = data
        self.cost_per_call = cost_per_call
        self.confidence = confidence
        self.coverage_key = coverage_key or (lambda r: r.get("email"))
        self.calls = 0

    def enrich(self, record: Record) -> EnrichmentResult:
        self.calls += 1
        key = self.coverage_key(record)
        payload = self.data.get(key or "", {})
        confidence = (
            {f: self.confidence for f in payload}
            if isinstance(self.confidence, float)
            else dict(self.confidence)
        )
        return EnrichmentResult(
            provider=self.name,
            fields=dict(payload),
            confidence=confidence,
            cost_credits=self.cost_per_call,
            matched=bool(payload),
        )


def enrich_entity(
    store: ObservationStore,
    entity_id: str,
    record: Record,
    providers: Sequence[EnrichmentProvider],
    *,
    source_record_id: str | None = None,
    ttl_overrides: dict | None = None,
) -> list[Observation]:
    """Run providers and append their claims as observations.

    Note what this function does *not* do: decide.  Two providers disagreeing
    about employee count both get recorded, the disagreement is visible by
    construction, and which one wins is the field policy's business.
    """
    written: list[Observation] = []
    for provider in providers:
        result = provider.enrich(record)
        if not result.matched:
            continue
        for name, value in result.fields.items():
            if value is None:
                continue
            written.append(
                store.append(
                    Observation(
                        entity_id=entity_id,
                        field=name,
                        value=value,
                        source=provider.name,
                        source_kind=SourceKind.ENRICHMENT,
                        source_record_id=source_record_id or record.source_record_id,
                        confidence=result.confidence.get(name, 0.5),
                        observed_at=result.observed_at,
                        expires_at=expires_at(name, result.observed_at, ttl_overrides),
                    )
                )
            )
    return written


def coverage(results: Iterable[EnrichmentResult]) -> float:
    """The number providers advertise: fraction that came back with *something*.

    Kept separate from accuracy on purpose.  "95% coverage" almost always means
    this, and only accuracy matters (section 10.1).
    """
    results = list(results)
    if not results:
        return 0.0
    return sum(1 for r in results if r.matched) / len(results)
