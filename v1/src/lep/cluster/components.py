"""Pairs to components (design.md section 6.1).

Pairwise scores give edges; entities are clusters.  The naive step is
connected components -- and on its own it is wrong, for the reason in
``guards.py``.  This module provides the mechanism; the guards decide whether
the result is allowed to become a merge.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Iterable, Sequence

from lep.identity.scorer import ScoredPair


class EdgeIndex:
    """Scored pairs, queryable in both directions."""

    def __init__(self, pairs: Iterable[ScoredPair] | Iterable[tuple[str, str, float]] = ()):
        self._scores: dict[tuple[str, str], float] = {}
        self._adjacent: dict[str, set[str]] = defaultdict(set)
        for pair in pairs:
            if isinstance(pair, ScoredPair):
                self.add(pair.left, pair.right, pair.probability)
            else:
                self.add(*pair)

    @staticmethod
    def _key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def add(self, a: str, b: str, score: float) -> None:
        if a == b:
            return
        key = self._key(a, b)
        # Keep the strongest evidence if the same pair arrives twice.
        self._scores[key] = max(score, self._scores.get(key, float("-inf")))
        self._adjacent[a].add(b)
        self._adjacent[b].add(a)

    def score(self, a: str, b: str) -> float | None:
        """None means "never compared", which is different from "scored low"."""
        if a == b:
            return 1.0
        return self._scores.get(self._key(a, b))

    def neighbours(self, node: str, above: float = float("-inf")) -> set[str]:
        return {
            other
            for other in self._adjacent.get(node, ())
            if (self.score(node, other) or float("-inf")) >= above
        }

    def nodes(self) -> set[str]:
        return set(self._adjacent)

    def edges(self, above: float = float("-inf")) -> list[tuple[str, str, float]]:
        return [
            (a, b, score)
            for (a, b), score in self._scores.items()
            if score >= above
        ]

    def within(self, cluster: Iterable[str]) -> list[tuple[str, str, float]]:
        members = set(cluster)
        return [
            (a, b, score)
            for (a, b), score in self._scores.items()
            if a in members and b in members
        ]

    def count_within(self, cluster: Iterable[str], above: float) -> int:
        members = sorted(set(cluster))
        return sum(
            1
            for a, b in combinations(members, 2)
            if (self.score(a, b) or float("-inf")) >= above
        )

    def any_within(self, cluster: Iterable[str], below: float) -> bool:
        """Is any *scored* internal pair strong evidence of being distinct?

        Pairs that were never compared do not count: blocking not generating a
        pair is absence of evidence, not evidence of absence.
        """
        members = sorted(set(cluster))
        for a, b in combinations(members, 2):
            score = self.score(a, b)
            if score is not None and score <= below:
                return True
        return False

    def __len__(self) -> int:
        return len(self._scores)


class UnionFind:
    def __init__(self, items: Iterable[str] = ()) -> None:
        self.parent: dict[str, str] = {item: item for item in items}
        self.rank: dict[str, int] = dict.fromkeys(self.parent, 0)

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        self.rank.setdefault(item, 0)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1

    def groups(self) -> list[set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for item in self.parent:
            out[self.find(item)].add(item)
        return list(out.values())


def connected_components(
    edges: EdgeIndex,
    threshold: float,
    *,
    nodes: Sequence[str] | None = None,
) -> list[set[str]]:
    """Transitive closure over edges at or above ``threshold``.

    This is the step that collapses chains, so nothing calls it without running
    the guards afterwards.
    """
    uf = UnionFind(nodes or edges.nodes())
    for a, b, score in edges.edges(above=threshold):
        uf.union(a, b)
    return sorted(uf.groups(), key=lambda c: (-len(c), sorted(c)))
