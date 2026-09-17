"""Persistence.

The reference backend is SQLite so the whole system runs with no services.
The schema is deliberately the same shape as the PostgreSQL one in
``docs/schema.postgres.sql`` (design.md section 4), so moving to Postgres is a
driver change, not a redesign.
"""

from lep.store.db import Database
from lep.store.entities import EntityStore
from lep.store.observations import ObservationStore
from lep.store.resolve import resolve, resolve_all

__all__ = ["Database", "EntityStore", "ObservationStore", "resolve", "resolve_all"]
