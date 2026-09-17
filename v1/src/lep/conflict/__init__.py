"""Conflict reconciliation (design.md section 8)."""

from lep.conflict.log import Conflict, ConflictLog
from lep.conflict.strategies import (
    Direction,
    FieldPolicy,
    PolicySet,
    ResolutionStrategy,
    resolve_observations,
)

__all__ = [
    "Conflict",
    "ConflictLog",
    "Direction",
    "FieldPolicy",
    "PolicySet",
    "ResolutionStrategy",
    "resolve_observations",
]
