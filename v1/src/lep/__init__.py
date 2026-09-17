"""Lead Enrichment Pipeline.

An entity resolution and sync service built on an append-only observation
store.  See ``docs/design.md`` for the design this implements; section
numbers referenced throughout the code point back at that document.

The governing principle (design.md section 5.4): a false merge is irreversible
data destruction and a privacy exposure, a false split is a duplicate row.
Everything in this package is arranged so that the system prefers the second
failure to the first.
"""

from lep.core.types import (
    Decision,
    Entity,
    EntityLink,
    Merge,
    Observation,
    ResolvedValue,
    SourceKind,
)

__all__ = [
    "Decision",
    "Entity",
    "EntityLink",
    "Merge",
    "Observation",
    "ResolvedValue",
    "SourceKind",
    "__version__",
]

__version__ = "0.1.0"
