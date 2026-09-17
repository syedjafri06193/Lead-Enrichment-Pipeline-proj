"""End-to-end wiring.

Everything in this package is usable on its own; this module is the assembled
version -- the object the CLI, the demo and the tests build when they want a
working system rather than one component.

The order of operations is the order the milestone ladder builds them
(design.md section 15), and it is not arbitrary:

1. field policy, before any sync code;
2. the observation store;
3. the budget manager, before any bulk operation;
4. ingestion with idempotency and ordering;
5. deterministic matching;
6. probabilistic matching, *measured*;
7. the review queue, before auto-merge;
8. clustering with guards;
9. sync, one direction then two;
10. conflict reconciliation, enrichment, privacy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from lep.budget.manager import ApiBudget, BudgetManager, Priority
from lep.cluster.components import EdgeIndex
from lep.cluster.guards import ClusterConfig, ClusterResult, resolve_clusters
from lep.conflict.log import ConflictLog
from lep.conflict.strategies import PolicySet
from lep.core.types import Decision, Observation, Record, SourceKind, utcnow
from lep.events.dlq import DeadLetterQueue
from lep.events.idempotency import IdempotencyStore
from lep.identity.blocking import BLOCKING_RULES, BlockingStats, candidate_pairs
from lep.identity.deterministic import deterministic_matches
from lep.identity.normalize import normalize_record
from lep.identity.policy import MatchPolicy, decide
from lep.identity.scorer import FellegiSunterScorer, ScoredPair
from lep.review.queue import ReviewQueue
from lep.store.db import Database
from lep.store.entities import EntityStore
from lep.store.observations import ObservationStore
from lep.store.resolve import current_record, resolve_all
from lep.sync.circuit import LoopDetector
from lep.sync.crm import FakeCrm
from lep.sync.echo import WriteLog
from lep.sync.engine import SyncEngine


def find_policy_path() -> Path:
    """Locate ``config/field-policy.yaml``.

    Checked in order: an explicit ``LEP_FIELD_POLICY`` path, the working
    directory, and the repository this module was installed from.  If none
    exists the error says so plainly rather than starting with a default
    policy -- a sync service running on a policy nobody wrote is exactly the
    thing milestone M0 exists to prevent.
    """
    import os

    candidates = [
        Path(os.environ["LEP_FIELD_POLICY"]) if os.environ.get("LEP_FIELD_POLICY") else None,
        Path.cwd() / "config" / "field-policy.yaml",
        Path(__file__).resolve().parents[2] / "config" / "field-policy.yaml",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    raise FileNotFoundError(
        "no field policy found: set LEP_FIELD_POLICY or create "
        "config/field-policy.yaml (see docs/field-policy.md)"
    )


DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "field-policy.yaml"


@dataclass
class MatchOutcome:
    """What matching decided, and what it refused to decide."""

    scored: list[ScoredPair] = field(default_factory=list)
    auto_merged: list[tuple[str, str]] = field(default_factory=list)
    to_review: list[ScoredPair] = field(default_factory=list)
    distinct: int = 0
    deterministic: int = 0
    edges: EdgeIndex | None = None
    blocking: BlockingStats = field(default_factory=BlockingStats)
    clusters: ClusterResult | None = None

    def summary(self) -> str:
        return (
            f"{self.deterministic} deterministic links, "
            f"{len(self.scored)} pairs scored, "
            f"{len(self.auto_merged)} auto-merged, "
            f"{len(self.to_review)} to review, {self.distinct} distinct"
        )


class Pipeline:
    def __init__(
        self,
        *,
        db: Database | None = None,
        policies: PolicySet | None = None,
        match_policy: MatchPolicy | None = None,
        cluster_config: ClusterConfig | None = None,
        clients: dict[str, FakeCrm] | None = None,
        budget: BudgetManager | None = None,
        clock: Callable[[], datetime] = utcnow,
        org_id: str = "org",
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.db = db or Database()
        self.clock = clock
        self.alert = alert or (lambda message: None)

        self.policies = policies or PolicySet.load(find_policy_path())
        problems = self.policies.validate()
        if problems:  # M0 is a gate, not a suggestion.
            raise ValueError("field policy problems: " + "; ".join(problems))

        self.match_policy = match_policy or MatchPolicy()
        self.cluster_config = cluster_config or ClusterConfig()

        self.observations = ObservationStore(self.db)
        self.entities = EntityStore(self.db)
        self.conflicts = ConflictLog(self.db)
        self.review = ReviewQueue(self.db)
        self.idempotency = IdempotencyStore(self.db)
        self.dlq = DeadLetterQueue(self.db, alert=self.alert)
        self.scorer = FellegiSunterScorer()

        self.clients = clients or {}
        self.budget = budget or BudgetManager(
            org_id,
            [
                ApiBudget(org_id, "salesforce", 15_000),
                ApiBudget(org_id, "hubspot", 625_000),
            ],
            clock=clock,
        )
        self.budget.attach_ledger(self.db)

        self.write_log = WriteLog(self.db, clock=clock)
        self.loop_detector = LoopDetector(self.db, alert=self.alert, clock=clock)
        self.engine = SyncEngine(
            observations=self.observations,
            entities=self.entities,
            policies=self.policies,
            clients=self.clients,
            write_log=self.write_log,
            loop_detector=self.loop_detector,
            budget=self.budget,
            conflict_log=self.conflicts,
            review_queue=self.review,
            clock=clock,
        )

    # -------------------------------------------------------------- intake

    def ingest_record(self, record: Record, *, entity_id: str | None = None) -> str:
        """Load a flat record as observations under one entity."""
        normalize_record(record)
        entity_id = entity_id or self.entities.entity_for_record(
            record.source, record.source_record_id
        )
        if entity_id is None:
            entity_id = self.entities.create(record.type).id
        self.entities.link(
            entity_id, record.source, record.source_record_id, confidence=1.0
        )
        record.entity_id = entity_id
        now = self.clock()
        for name, value in record.fields.items():
            if value in (None, ""):
                continue
            policy = self.policies.for_field(name)
            self.observations.append(
                Observation(
                    entity_id=entity_id,
                    field=name,
                    value=value,
                    source=record.source,
                    source_kind=SourceKind.CRM,
                    source_record_id=record.source_record_id,
                    observed_at=now,
                    expires_at=now + policy.ttl if policy.ttl else None,
                )
            )
        return entity_id

    # ------------------------------------------------------------ matching

    def match(
        self,
        records: Sequence[Record],
        *,
        train: bool = True,
        labels: Iterable[tuple[str, str, bool]] = (),
        cluster: bool = True,
    ) -> MatchOutcome:
        """Deterministic, then blocking, then scoring, then guarded clustering."""
        for record in records:
            normalize_record(record)

        outcome = MatchOutcome()
        edges = EdgeIndex()

        # 1. Deterministic rules resolve most of the volume (section 5.1).
        deterministic = deterministic_matches(records)
        outcome.deterministic = len(deterministic)
        for match in deterministic:
            edges.add(match.left, match.right, match.confidence)

        # 2. Blocking: the only way this runs at all (section 5.2).
        pairs = candidate_pairs(records, BLOCKING_RULES, stats=outcome.blocking)

        # 3. Probabilistic scoring (section 5.3).
        labels = list(labels) or self.review.labels()
        if train and labels:
            self.scorer.fit_supervised(records, labels)
        elif train:
            self.scorer.fit_em(records, sorted(pairs))
        else:
            self.scorer.fit_term_frequencies(records)

        outcome.scored = self.scorer.score_pairs(records, sorted(pairs))

        # 4. Banded decisions.  Auto-merge is off until measurement says
        #    otherwise, so in a new deployment everything lands in review.
        for pair in outcome.scored:
            edges.add(pair.left, pair.right, pair.probability)
            decision = decide(pair.probability, self.match_policy)
            if decision is Decision.AUTO_MERGE:
                outcome.auto_merged.append(pair.pair)
            elif decision is Decision.REVIEW:
                outcome.to_review.append(pair)
                self.review.enqueue_pair(pair)
            else:
                outcome.distinct += 1

        # 5. Clustering with guards (section 6.3).
        if cluster:
            outcome.clusters = resolve_clusters(
                edges,
                self.cluster_config,
                nodes=[r.key for r in records],
            )
            for members, verdict in outcome.clusters.review:
                self.review.enqueue_cluster(members, verdict.value)

        outcome.edges = edges
        return outcome

    def apply_merges(
        self,
        outcome: MatchOutcome,
        *,
        decided_by: str = "auto:v1",
        edges: EdgeIndex | None = None,
    ) -> int:
        """Merge only what a validated cluster supports.

        Three conditions, all necessary:

        1. auto-merge is switched on at all -- and it is off until measured
           precision justifies it (section 5.4);
        2. the cluster passed every guard;
        3. every scored pair inside the cluster clears the auto-merge
           threshold, not merely the clustering threshold.

        A cluster built from 0.85 edges is a fine *candidate*; merging on it
        would be spending the precision bar the rest of the design exists to
        protect.
        """
        if outcome.clusters is None or not self.match_policy.auto_merge_enabled:
            return 0
        merged = 0
        edges = edges or outcome.edges
        flagged = {frozenset(members) for members, _ in outcome.clusters.review}
        for cluster in outcome.clusters.clusters:
            if len(cluster) < 2 or frozenset(cluster) in flagged:
                continue
            if edges is not None and _weakest_internal_edge(cluster, edges) < (
                self.match_policy.auto_merge_threshold
            ):
                self.review.enqueue_cluster(cluster, "below auto-merge threshold")
                continue
            entity_ids = []
            for key in sorted(cluster):
                source, record_id = key.split(":", 1)
                entity_id = self.entities.entity_for_record(source, record_id)
                if entity_id:
                    entity_ids.append(entity_id)
            unique = list(dict.fromkeys(entity_ids))
            if len(unique) < 2:
                continue
            survivor = unique[0]
            for other in unique[1:]:
                self.entities.merge(survivor, other, decided_by=decided_by)
                merged += 1
        return merged

    # ------------------------------------------------------------ readback

    def record_for(self, entity_id: str, *, at: datetime | None = None) -> dict[str, Any]:
        return current_record(
            self.observations, entity_id, self.policies, at=at or self.clock()
        )

    def resolved(self, entity_id: str, *, at: datetime | None = None):
        return resolve_all(
            self.observations,
            entity_id,
            self.policies,
            at=at or self.clock(),
            conflict_log=self.conflicts,
        )

    def push_all(self, entity_id: str, *, priority: Priority = Priority.INCREMENTAL) -> int:
        return sum(
            self.engine.push(entity_id, field_name, priority=priority)
            for field_name in self.observations.fields_for(entity_id)
        )

    # ----------------------------------------------------------- reporting

    def health(self) -> dict[str, Any]:
        """The handful of numbers worth watching in production."""
        return {
            "entities": len(self.entities.all_ids()),
            "observations": self.observations.count(),
            "conflicts": self.conflicts.count(),
            "top_conflicting_fields": self.conflicts.top_conflicting_fields(5),
            "review_depth": self.review.depth(),
            "dlq_depth": self.dlq.depth(),
            "quarantined": len(self.loop_detector.quarantined()),
            "loop_breaker_trips": self.loop_detector.trips,
            "budget": self.budget.snapshot(),
            "sync": self.engine.stats.summary(),
        }


def _weakest_internal_edge(cluster: set[str], edges: EdgeIndex) -> float:
    """Lowest score among the pairs inside a cluster that were actually scored."""
    scores = [score for _, _, score in edges.within(cluster)]
    return min(scores) if scores else 0.0
