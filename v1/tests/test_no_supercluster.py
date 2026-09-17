"""The first of the three tests that matter most (design.md section 17.1).

Matching is not transitive.  A chain of weak links must not collapse into one
entity, because the symptom of that failure -- a single record holding forty
email addresses -- is discovered months later, if ever.
"""

from __future__ import annotations

import pytest

from lep.cluster.components import EdgeIndex, connected_components
from lep.cluster.guards import (
    ClusterConfig,
    ClusterVerdict,
    cohesion,
    distribution_shift,
    resolve_clusters,
    validate_cluster,
)
from lep.testing import build_chain, chain_records


def test_no_supercluster_from_chain():
    """A <-0.85-> B <-0.85-> C <-0.85-> D, with A and D clearly distinct.

    Must NOT become one cluster.
    """
    edges = build_chain(n=8, edge_score=0.85)
    result = resolve_clusters(edges, ClusterConfig())
    assert result.largest <= 3, "chain collapsed into a supercluster"


def test_connected_components_alone_would_have_collapsed_it():
    """The control: this is what the guards are protecting against."""
    edges = build_chain(n=8, edge_score=0.85)
    naive = connected_components(edges, threshold=0.80)
    assert max(len(c) for c in naive) == 8


def test_cohesion_of_a_chain_is_about_two_over_n():
    edges = build_chain(n=10, edge_score=0.85)
    members = {f"r{i}" for i in range(10)}
    assert cohesion(members, edges, 0.80) == pytest.approx(9 / 45, abs=0.01)


def test_cohesion_of_a_real_cluster_is_near_one():
    edges = EdgeIndex(
        (a, b, 0.97) for a in "abcd" for b in "abcd" if a < b
    )
    assert cohesion(set("abcd"), edges, 0.80) == 1.0
    assert validate_cluster(set("abcd"), edges) is ClusterVerdict.OK


def test_genuine_duplicates_still_merge():
    """A guard that blocks every merge is not a guard, it is an outage."""
    edges = EdgeIndex((a, b, 0.99) for a in "wxyz" for b in "wxyz" if a < b)
    result = resolve_clusters(edges, ClusterConfig())
    assert [sorted(c) for c in result.clusters] == [["w", "x", "y", "z"]]
    assert not result.review


def test_oversized_cluster_goes_to_review_never_to_auto_merge():
    members = [f"n{i}" for i in range(12)]
    edges = EdgeIndex(
        (a, b, 0.99) for i, a in enumerate(members) for b in members[i + 1 :]
    )
    result = resolve_clusters(edges, ClusterConfig(max_size=10))
    assert any(
        verdict is ClusterVerdict.TOO_LARGE for _, verdict in result.review
    ), "a cluster over max_size must be reviewed"


def test_contradicted_cluster_is_not_merged():
    """Strong evidence that two members are distinct outranks the chain."""
    edges = EdgeIndex([("x", "y", 0.95), ("y", "z", 0.95), ("x", "z", 0.05)])
    assert validate_cluster({"x", "y", "z"}, edges) is not ClusterVerdict.OK
    result = resolve_clusters(edges, ClusterConfig())
    assert result.largest <= 2


def test_never_compared_is_not_the_same_as_scored_low():
    """Blocking not generating a pair is absence of evidence."""
    edges = EdgeIndex([("a", "b", 0.99), ("b", "c", 0.99), ("a", "c", 0.99)])
    assert edges.score("a", "d") is None
    assert not edges.any_within({"a", "b", "c"}, below=0.2)


def test_cluster_size_distribution_shift_alerts():
    """The earliest warning that a threshold moved (section 6.3)."""
    before = {1: 900, 2: 90, 3: 10}
    after = {1: 700, 2: 150, 3: 100, 40: 1}
    alerts = distribution_shift(before, after)
    assert alerts
    assert any("largest cluster grew" in a for a in alerts)


def test_records_that_chain_do_not_merge_end_to_end(pipeline):
    """Same property, through the whole pipeline rather than on raw edges."""
    records = chain_records(8)
    outcome = pipeline.match(records, train=False)
    assert outcome.clusters is not None
    assert outcome.clusters.largest <= 3
