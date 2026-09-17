"""Cluster guards (design.md section 6.2, 6.3).

Matching is **not transitive**.  A can match B, B can match C, and A can be
clearly distinct from C::

    "J. Smith, Acme" -- 0.85 -- "John Smith, Acme" -- 0.91 -- "John Smith, Acme
    Corp" -- 0.87 -- "Jon Smith, Acme"

Connected components merges all four.  Add a few hundred records and a chain of
weak links collapses a large population of distinct people into one entity --
the characteristic symptom being a single record with forty email addresses and
a job title from an industry nobody in the cluster works in.

Cohesion is the key metric.  A four-record chain has three edges out of six
possible pairs: cohesion 0.5.  A genuine cluster of four duplicates has close
to six.  Requiring cohesion above ~0.7 kills chains while preserving real
clusters.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import Iterable, Sequence

from lep.cluster.components import EdgeIndex, connected_components


class ClusterVerdict(str, Enum):
    OK = "ok"
    TOO_LARGE = "too_large"
    LOW_COHESION = "low_cohesion"
    CONTRADICTED = "contradicted"


@dataclass(frozen=True)
class ClusterConfig:
    """Starting values from section 6.3."""

    #: 10 for people, 25 for companies.
    max_size: int = 10
    min_cohesion: float = 0.7
    contradiction_threshold: float = 0.2
    edge_threshold: float = 0.80

    @classmethod
    def for_type(cls, entity_type: str) -> "ClusterConfig":
        return cls(max_size=25 if entity_type == "company" else 10)


def cohesion(cluster: Iterable[str], edges: EdgeIndex, threshold: float) -> float:
    """Fraction of possible internal pairs that actually match.

    Chain of n records: about 2/n.  Genuine cluster: near 1.0.
    """
    members = sorted(set(cluster))
    n = len(members)
    if n < 2:
        return 1.0
    possible = n * (n - 1) // 2
    actual = sum(
        1
        for a, b in combinations(members, 2)
        if (edges.score(a, b) or float("-inf")) >= threshold
    )
    return actual / possible


def validate_cluster(
    cluster: set[str], edges: EdgeIndex, cfg: ClusterConfig = ClusterConfig()
) -> ClusterVerdict:
    n = len(cluster)
    if n <= 1:
        return ClusterVerdict.OK

    if n > cfg.max_size:
        return ClusterVerdict.TOO_LARGE          # -> review, never auto-merge

    if cohesion(cluster, edges, cfg.edge_threshold) < cfg.min_cohesion:
        return ClusterVerdict.LOW_COHESION       # -> split or review

    # No pair inside a cluster may be strongly evidenced as distinct.
    if edges.any_within(cluster, below=cfg.contradiction_threshold):
        return ClusterVerdict.CONTRADICTED
    return ClusterVerdict.OK


def split_cluster(
    cluster: set[str], edges: EdgeIndex, cfg: ClusterConfig = ClusterConfig()
) -> list[set[str]]:
    """Break a failing cluster along its weakest links until the parts pass.

    Repeatedly removes the lowest-scoring internal edge and re-runs connected
    components on what is left.  On a chain this peels off pairs; on a genuine
    dense cluster it stops immediately, because a dense cluster passes on the
    first check.

    Splitting rather than merging is the safe direction of failure: a split
    that should not have happened leaves two rows a human can rejoin, and that
    is the whole asymmetry (section 5.4).
    """
    if len(cluster) <= 1 or validate_cluster(cluster, edges, cfg) is ClusterVerdict.OK:
        return [set(cluster)]

    internal = sorted(edges.within(cluster), key=lambda e: e[2])
    if not internal:
        return [{member} for member in sorted(cluster)]

    surviving = list(internal)
    while surviving:
        weakest = surviving[0][2]
        tied = [e for e in surviving if e[2] <= weakest + 1e-12]
        # Among equally weak links, cut the one that splits the cluster most
        # evenly.  On a uniform chain that is the middle link, so the chain
        # halves instead of shedding one record at a time.
        best: tuple[float, list[set[str]], tuple[str, str, float]] | None = None
        for candidate in tied:
            remaining = [e for e in surviving if e is not candidate]
            sub = EdgeIndex((a, b, s) for a, b, s in remaining)
            parts = [p for p in connected_components(sub, cfg.edge_threshold,
                                                     nodes=sorted(cluster)) if p]
            if len(parts) < 2:
                continue
            balance = min(len(p) for p in parts) / max(len(p) for p in parts)
            if best is None or balance > best[0]:
                best = (balance, parts, candidate)

        if best is not None:
            _, parts, _ = best
            out: list[set[str]] = []
            for part in parts:
                out.extend(
                    split_cluster(part, edges, cfg) if part != cluster else [part]
                )
            return out

        # No single cut at this strength disconnects anything; drop them all
        # and try the next-weakest band.
        surviving = [e for e in surviving if e not in tied]

    return [{member} for member in sorted(cluster)]


@dataclass
class ClusterResult:
    """Clusters, plus the ones a human has to look at."""

    clusters: list[set[str]] = field(default_factory=list)
    verdicts: dict[frozenset[str], ClusterVerdict] = field(default_factory=dict)
    #: Clusters that failed a guard.  These go to review, never to auto-merge.
    review: list[tuple[set[str], ClusterVerdict]] = field(default_factory=list)
    split_count: int = 0

    @property
    def largest(self) -> int:
        return max((len(c) for c in self.clusters), default=0)

    def size_distribution(self) -> dict[int, int]:
        return dict(sorted(Counter(len(c) for c in self.clusters).items()))


def resolve_clusters(
    edges: EdgeIndex,
    cfg: ClusterConfig = ClusterConfig(),
    *,
    nodes: Sequence[str] | None = None,
    split: bool = True,
) -> ClusterResult:
    """Components, then guards, then splitting.

    Anything still failing a guard after the split is routed to review rather
    than merged.  Clusters failing a guard never auto-merge -- that is the rule
    the rest of section 6 exists to enforce.
    """
    result = ClusterResult()
    for component in connected_components(edges, cfg.edge_threshold, nodes=nodes):
        verdict = validate_cluster(component, edges, cfg)
        if verdict is ClusterVerdict.OK:
            result.clusters.append(component)
            result.verdicts[frozenset(component)] = verdict
            continue

        if not split:
            result.clusters.append(component)
            result.verdicts[frozenset(component)] = verdict
            result.review.append((component, verdict))
            continue

        parts = split_cluster(component, edges, cfg)
        if len(parts) > 1:
            result.split_count += 1
        for part in parts:
            part_verdict = validate_cluster(part, edges, cfg)
            result.clusters.append(part)
            result.verdicts[frozenset(part)] = part_verdict
            if part_verdict is not ClusterVerdict.OK:
                result.review.append((part, part_verdict))
        # The original component is worth a human look even after splitting:
        # something generated a chain, and that is usually a model problem.
        if len(component) > cfg.max_size:
            result.review.append((component, verdict))

    result.clusters.sort(key=lambda c: (-len(c), sorted(c)))
    return result


def size_distribution(clusters: Iterable[Iterable[str]]) -> dict[int, int]:
    return dict(sorted(Counter(len(set(c)) for c in clusters).items()))


def distribution_shift(
    before: dict[int, int], after: dict[int, int], *, tolerance: float = 0.25
) -> list[str]:
    """Compare cluster-size distributions across a model change.

    Section 6.3: "look at the distribution of cluster sizes after every model
    change.  A sudden shift toward large clusters means the threshold moved or
    the data changed, and it's the earliest warning you'll get."
    """
    alerts: list[str] = []
    before_total = sum(before.values()) or 1
    after_total = sum(after.values()) or 1

    def mass_above(dist: dict[int, int], size: int, total: int) -> float:
        return sum(n for s, n in dist.items() if s >= size) / total

    for size in (2, 5, 10):
        was = mass_above(before, size, before_total)
        now = mass_above(after, size, after_total)
        if now > was * (1 + tolerance) + 1e-9:
            alerts.append(
                f"clusters of size >= {size} rose from {was:.3%} to {now:.3%} of all "
                "clusters -- check the threshold and the model before shipping"
            )
    biggest_before = max(before, default=0)
    biggest_after = max(after, default=0)
    if biggest_after > biggest_before:
        alerts.append(
            f"largest cluster grew from {biggest_before} to {biggest_after} records"
        )
    return alerts
