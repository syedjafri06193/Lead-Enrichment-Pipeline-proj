"""Field-level source-of-truth policy (design.md section 8.2).

There is no correct generic conflict resolution algorithm.  What works is
policy declared per field, which is configuration rather than cleverness.

The one rule worth memorising: **last-write-wins is fine within a single
source and broken across sources**.  Within one source the timestamps come
from one clock; across sources they come from clocks we do not control, and
"last" is not "correct" -- a 3am automation is last for ten thousand records.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from lep.core.types import Observation, ResolvedValue, SourceKind


class ResolutionStrategy(str, Enum):
    SOURCE_PRIORITY = "source_priority"   # ranked sources; highest wins
    MOST_RECENT = "most_recent"           # LWW, within ONE source only
    MOST_CONFIDENT = "most_confident"     # highest-confidence observation
    HUMAN_WINS = "human_wins"             # any user edit beats any automated one
    NEVER_OVERWRITE = "never_overwrite"   # first non-null value sticks
    MANUAL = "manual"                     # always escalate


class Direction(str, Enum):
    """Sync direction, declared per field (design.md section 7.1).

    Most fields end up one-directional, and that is the right outcome: every
    bidirectional field is a conflict you have volunteered to resolve.
    """

    NONE = "none"
    BIDIRECTIONAL = "bidirectional"
    SF_TO_HUBSPOT = "sf_to_hubspot"
    HUBSPOT_TO_SF = "hubspot_to_sf"
    SF_ONLY = "sf_only"
    HUBSPOT_ONLY = "hubspot_only"
    ENRICHMENT_TO_ALL = "enrichment_to_all"


#: Canonical system names used by the sync engine.
SALESFORCE = "salesforce"
HUBSPOT = "hubspot"
ENRICHMENT = "enrichment"


@dataclass(frozen=True)
class FieldPolicy:
    """Everything the system is allowed to decide about one field."""

    field: str
    master: str | None = None
    direction: Direction = Direction.NONE
    strategy: ResolutionStrategy = ResolutionStrategy.HUMAN_WINS
    source_ranking: tuple[str, ...] = ()
    ttl_days: int | None = None
    #: Fallback used once HUMAN_WINS finds no human observation.
    fallback: ResolutionStrategy = ResolutionStrategy.SOURCE_PRIORITY

    @property
    def ttl(self) -> timedelta | None:
        return None if self.ttl_days is None else timedelta(days=self.ttl_days)

    # ------------------------------------------------------------ direction

    def writes_to(self, target: str) -> bool:
        """May the sync engine write this field into ``target``?"""
        d = self.direction
        if d is Direction.NONE:
            return False
        if d is Direction.BIDIRECTIONAL:
            return target in (SALESFORCE, HUBSPOT)
        if d is Direction.SF_TO_HUBSPOT:
            return target == HUBSPOT
        if d is Direction.HUBSPOT_TO_SF:
            return target == SALESFORCE
        if d is Direction.ENRICHMENT_TO_ALL:
            return target in (SALESFORCE, HUBSPOT)
        # *_ONLY: authoritative in one system and never written from outside.
        return False

    def accepts_from(self, source: str, kind: SourceKind | None = None) -> bool:
        """May an observation from ``source`` win the resolution?

        Every observation is *recorded* regardless -- they are evidence, and
        discarding them would break replay.  This governs which of them are
        eligible to be the resolved value.

        The rule is about mirrors: under ``sf_to_hubspot`` the HubSpot copy of
        the field is our own write coming back, so letting it win would launder
        our write into a fact.  A human edit is never a mirror, so
        ``SourceKind.USER`` is always eligible.
        """
        if kind is SourceKind.USER or source == "user":
            return True
        if source == ENRICHMENT:
            return True
        d = self.direction
        if d is Direction.NONE:
            return True          # not synced, but still resolvable
        if d is Direction.SF_ONLY:
            return source == SALESFORCE
        if d is Direction.HUBSPOT_ONLY:
            return source == HUBSPOT
        if d is Direction.SF_TO_HUBSPOT:
            return source == SALESFORCE
        if d is Direction.HUBSPOT_TO_SF:
            return source == HUBSPOT
        if d is Direction.ENRICHMENT_TO_ALL:
            return False         # the CRM copies are mirrors of our write
        return True

    # ----------------------------------------------------------- resolution

    def resolve(self, obs: Iterable[Observation]) -> ResolvedValue:
        return resolve_observations(list(obs), self)


def _newest(obs: list[Observation]) -> Observation:
    return max(obs, key=lambda o: (o.observed_at, o.id or 0))


def _oldest(obs: list[Observation]) -> Observation:
    return min(obs, key=lambda o: (o.observed_at, o.id or 0))


def _result(winner: Observation, reason: str, candidates: list[Observation],
            needs_review: bool = False) -> ResolvedValue:
    return ResolvedValue(
        value=winner.value,
        reason=reason,
        source=winner.source,
        observed_at=winner.observed_at,
        winning_observation=winner,
        candidates=tuple(candidates),
        needs_review=needs_review,
    )


def resolve_observations(
    observations: list[Observation], policy: FieldPolicy
) -> ResolvedValue:
    """Pure function: observations plus policy gives a value.

    Callers filter expiry before calling (``ObservationStore.live``); this
    function only implements the policy.  Being pure is the point -- change the
    policy, replay the history, get a different current value with nothing
    lost (section 4.1).
    """
    live = [o for o in observations if o.value is not None]
    if not live:
        return ResolvedValue(None, reason="no live observations", candidates=())

    # Only observations the direction policy accepts may win.  The rest stay
    # in the candidate list so the conflict log still sees them.
    eligible = [o for o in live if policy.accepts_from(o.source, o.source_kind)] or live

    strategy = policy.strategy

    if strategy is ResolutionStrategy.MANUAL:
        winner = _newest(eligible)
        return _result(winner, "manual policy: escalated to review", live, needs_review=True)

    if strategy is ResolutionStrategy.HUMAN_WINS:
        human = [o for o in eligible if o.source_kind is SourceKind.USER]
        if human:
            # Enrichment fills gaps; it does not correct people (section 10.4).
            return _result(_newest(human), "human edit", live)
        strategy = policy.fallback

    if strategy is ResolutionStrategy.NEVER_OVERWRITE:
        return _result(_oldest(eligible), "first value sticks", live)

    if strategy is ResolutionStrategy.MOST_CONFIDENT:
        best = max(eligible, key=lambda o: (o.confidence, o.observed_at, o.id or 0))
        return _result(best, f"highest confidence ({best.confidence:.2f})", live)

    if strategy is ResolutionStrategy.MOST_RECENT:
        sources = {o.source for o in eligible}
        if len(sources) == 1:
            # One clock, so recency is meaningful.
            return _result(_newest(eligible), "most recent within source", live)
        # More than one clock.  Prefer an explicit ranking; otherwise say
        # plainly that we cannot order these and escalate.
        if policy.source_ranking:
            ranked = _by_source_priority(eligible, policy.source_ranking)
            if ranked is not None:
                return _result(
                    ranked,
                    f"most_recent across sources: fell back to ranking ({ranked.source})",
                    live,
                    needs_review=True,
                )
        return _result(
            _newest(eligible),
            "most_recent across sources: clocks are not comparable, escalated",
            live,
            needs_review=True,
        )

    # SOURCE_PRIORITY, the sane default.
    ranking = policy.source_ranking or ((policy.master,) if policy.master else ())
    winner = _by_source_priority(eligible, ranking)
    if winner is not None:
        return _result(winner, f"source priority: {winner.source}", live)
    # No ranked source present.  Recency within a single source is still fine.
    sources = {o.source for o in eligible}
    if len(sources) == 1:
        return _result(_newest(eligible), "single source, most recent", live)
    best = max(eligible, key=lambda o: (o.confidence, o.observed_at, o.id or 0))
    return _result(
        best,
        "no ranked source present: highest confidence, escalated",
        live,
        needs_review=True,
    )


def _by_source_priority(
    observations: list[Observation], ranking: Iterable[str]
) -> Observation | None:
    for source in ranking:
        if not source:
            continue
        match = [o for o in observations if o.source == source]
        if match:
            # LWW is acceptable WITHIN one source -- same clock.
            return _newest(match)
    return None


# --------------------------------------------------------------------- config


DEFAULT_POLICY = FieldPolicy(
    field="*",
    strategy=ResolutionStrategy.HUMAN_WINS,
    fallback=ResolutionStrategy.SOURCE_PRIORITY,
    source_ranking=(SALESFORCE, HUBSPOT, ENRICHMENT),
    direction=Direction.NONE,
)


@dataclass
class PolicySet:
    """All field policies for one object type, loaded from YAML.

    See ``config/field-policy.yaml`` and ``docs/field-policy.md``.  M0 in the
    milestone ladder exists because this file should be written, and signed off
    by the data owner, before any sync code runs.
    """

    objects: dict[str, dict[str, FieldPolicy]] = dc_field(default_factory=dict)
    default: FieldPolicy = DEFAULT_POLICY

    def for_field(self, field: str, object_type: str = "contact") -> FieldPolicy:
        policy = self.objects.get(object_type, {}).get(field)
        if policy is not None:
            return policy
        return FieldPolicy(
            field=field,
            master=self.default.master,
            direction=self.default.direction,
            strategy=self.default.strategy,
            source_ranking=self.default.source_ranking,
            ttl_days=self.default.ttl_days,
            fallback=self.default.fallback,
        )

    def fields(self, object_type: str = "contact") -> list[str]:
        return sorted(self.objects.get(object_type, {}))

    def synced_fields(self, target: str, object_type: str = "contact") -> list[str]:
        return [
            name
            for name, policy in sorted(self.objects.get(object_type, {}).items())
            if policy.writes_to(target)
        ]

    def ttl_map(self, object_type: str = "contact") -> dict[str, timedelta | None]:
        return {
            name: policy.ttl
            for name, policy in self.objects.get(object_type, {}).items()
        }

    # ------------------------------------------------------------- loading

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PolicySet:
        objects: dict[str, dict[str, FieldPolicy]] = {}
        for object_type, spec in (data.get("objects") or {}).items():
            fields: dict[str, FieldPolicy] = {}
            for name, raw in (spec.get("fields") or {}).items():
                raw = raw or {}
                fields[name] = FieldPolicy(
                    field=name,
                    master=raw.get("master"),
                    direction=Direction(raw.get("direction", "none")),
                    strategy=ResolutionStrategy(raw.get("strategy", "human_wins")),
                    source_ranking=tuple(raw.get("source_ranking", ()) or ()),
                    ttl_days=raw.get("ttl_days"),
                    fallback=ResolutionStrategy(
                        raw.get("fallback", "source_priority")
                    ),
                )
            objects[object_type] = fields

        default_raw = data.get("default") or {}
        default = FieldPolicy(
            field="*",
            master=default_raw.get("master"),
            direction=Direction(default_raw.get("direction", "none")),
            strategy=ResolutionStrategy(default_raw.get("strategy", "human_wins")),
            source_ranking=tuple(
                default_raw.get("source_ranking", (SALESFORCE, HUBSPOT, ENRICHMENT))
            ),
            ttl_days=default_raw.get("ttl_days"),
            fallback=ResolutionStrategy(default_raw.get("fallback", "source_priority")),
        )
        return cls(objects=objects, default=default)

    @classmethod
    def load(cls, path: str | Path) -> PolicySet:
        import yaml  # imported here so the core has no hard YAML dependency

        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_mapping(yaml.safe_load(handle) or {})

    def validate(self) -> list[str]:
        """Return human-readable problems with the policy.

        Run this at startup.  A policy that declares a master nobody ranks, or
        a bidirectional field with no conflict strategy, is a conflict waiting
        to be discovered in production instead.
        """
        problems: list[str] = []
        for object_type, fields in self.objects.items():
            for name, policy in fields.items():
                where = f"{object_type}.{name}"
                if policy.direction is Direction.BIDIRECTIONAL:
                    if policy.strategy is ResolutionStrategy.MOST_RECENT and not policy.source_ranking:
                        problems.append(
                            f"{where}: bidirectional with cross-source most_recent and no "
                            "source_ranking -- this is LWW across clocks you do not control"
                        )
                if (
                    policy.strategy is ResolutionStrategy.SOURCE_PRIORITY
                    and not policy.source_ranking
                    and not policy.master
                ):
                    problems.append(
                        f"{where}: source_priority with no ranking and no master"
                    )
                if (
                    policy.master
                    and policy.source_ranking
                    and policy.master not in policy.source_ranking
                ):
                    problems.append(
                        f"{where}: master {policy.master!r} is not in source_ranking"
                    )
        return problems
