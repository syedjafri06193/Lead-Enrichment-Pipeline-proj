"""Clustering with guards (design.md section 6)."""

from lep.cluster.components import EdgeIndex, connected_components
from lep.cluster.guards import (
    ClusterConfig,
    ClusterResult,
    ClusterVerdict,
    cohesion,
    distribution_shift,
    resolve_clusters,
    size_distribution,
    split_cluster,
    validate_cluster,
)

__all__ = [
    "ClusterConfig",
    "ClusterResult",
    "ClusterVerdict",
    "EdgeIndex",
    "cohesion",
    "connected_components",
    "distribution_shift",
    "resolve_clusters",
    "size_distribution",
    "split_cluster",
    "validate_cluster",
]
