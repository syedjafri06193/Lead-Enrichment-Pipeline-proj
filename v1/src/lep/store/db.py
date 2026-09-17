"""SQLite backing store and schema.

Every table here has a PostgreSQL counterpart in ``docs/schema.postgres.sql``.
The only interesting difference is JSONB vs. TEXT-holding-JSON; the access
patterns are identical.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Section 4.1: append-only, bitemporal.  Nothing in this table is ever
-- UPDATEd except superseded_by, and nothing is ever DELETEd except by erasure.
CREATE TABLE IF NOT EXISTS observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id        TEXT NOT NULL,
    field            TEXT NOT NULL,
    value            TEXT NOT NULL,          -- JSON
    source           TEXT NOT NULL,
    source_kind      TEXT NOT NULL,
    source_record_id TEXT,
    confidence       REAL NOT NULL DEFAULT 1.0,
    observed_at      TEXT NOT NULL,          -- source system's clock
    recorded_at      TEXT NOT NULL,          -- our clock
    expires_at       TEXT,
    superseded_by    INTEGER REFERENCES observations(id)
);
CREATE INDEX IF NOT EXISTS observations_lookup
    ON observations (entity_id, field, observed_at DESC);
CREATE INDEX IF NOT EXISTS observations_source
    ON observations (source, source_record_id);

CREATE TABLE IF NOT EXISTS entities (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_links (
    entity_id        TEXT NOT NULL REFERENCES entities(id),
    source           TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    confidence       REAL NOT NULL,
    linked_at        TEXT NOT NULL,
    linked_by        TEXT NOT NULL,
    PRIMARY KEY (source, source_record_id)
);
CREATE INDEX IF NOT EXISTS entity_links_entity ON entity_links (entity_id);

-- Section 4.2: merges are recorded, never destructive.
CREATE TABLE IF NOT EXISTS merges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    surviving_id  TEXT NOT NULL,
    merged_id     TEXT NOT NULL,
    score         REAL,
    decided_by    TEXT NOT NULL,
    decided_at    TEXT NOT NULL,
    reverted_at   TEXT,
    revert_reason TEXT
);
CREATE INDEX IF NOT EXISTS merges_merged ON merges (merged_id);
CREATE INDEX IF NOT EXISTS merges_surviving ON merges (surviving_id);

-- Section 9.1: at-least-once delivery.
CREATE TABLE IF NOT EXISTS event_claims (
    key        TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Section 7.2: the write log, the robust half of echo suppression.
CREATE TABLE IF NOT EXISTS write_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target     TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    value_hash TEXT NOT NULL,
    written_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS write_log_lookup
    ON write_log (target, record_id, field, written_at DESC);

-- Section 16.3: input to the loop circuit breaker.
CREATE TABLE IF NOT EXISTS change_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    field      TEXT,
    seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS change_log_lookup
    ON change_log (source, record_id, seen_at DESC);

CREATE TABLE IF NOT EXISTS quarantine (
    source     TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (source, record_id)
);

-- Section 8.3: log every conflict, including the auto-resolved ones.
CREATE TABLE IF NOT EXISTS conflicts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id         TEXT NOT NULL,
    field             TEXT NOT NULL,
    candidates        TEXT NOT NULL,       -- JSON
    resolved_to       TEXT,                -- JSON
    resolution_reason TEXT NOT NULL,
    detected_at       TEXT NOT NULL,
    reviewed          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS conflicts_field ON conflicts (field, detected_at DESC);

-- Section 6 / 5.4: everything routes through here before any auto-merge.
CREATE TABLE IF NOT EXISTS review_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- pair | cluster | delete
    left_key    TEXT NOT NULL,
    right_key   TEXT,
    score       REAL,
    payload     TEXT NOT NULL,            -- JSON
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL,            -- pending | decided | deferred
    created_at  TEXT NOT NULL,
    priority    REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS review_items_status ON review_items (status, priority DESC);

CREATE TABLE IF NOT EXISTS review_decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES review_items(id),
    model_score  REAL,
    human_match  INTEGER NOT NULL,
    reviewer     TEXT NOT NULL,
    note         TEXT,
    decided_at   TEXT NOT NULL
);

-- Section 11.2: without this, the next backfill resurrects the person.
CREATE TABLE IF NOT EXISTS erasure_suppression (
    key        TEXT PRIMARY KEY,          -- entity id or normalized identifier
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS erasure_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id  TEXT NOT NULL,
    reason     TEXT NOT NULL,
    report     TEXT NOT NULL,             -- JSON
    erased_at  TEXT NOT NULL
);

-- Section 9.3
CREATE TABLE IF NOT EXISTS dlq (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL,             -- JSON
    error      TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS event_attempts (
    key      TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL
);

-- Section 12.2: backfill resumes from here.
CREATE TABLE IF NOT EXISTS checkpoints (
    name       TEXT PRIMARY KEY,
    position   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Section 3.3: consumption is read from provider headers, but we keep our own
-- ledger too so spend is attributable per job.
CREATE TABLE IF NOT EXISTS api_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id    TEXT NOT NULL,
    provider  TEXT NOT NULL,
    calls     INTEGER NOT NULL,
    priority  TEXT NOT NULL,
    note      TEXT,
    at        TEXT NOT NULL
);
"""


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


class Database:
    """Thin SQLite wrapper.

    Connections are per-thread because the sync engine and the event worker run
    concurrently in the demo; SQLite objects are not shareable across threads.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        if path == ":memory:":
            # A single shared connection, since each new one would get its own
            # empty database.
            self._shared = self._connect()
        else:
            self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        self._local.conn = conn
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
        return conn

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.execute(sql, params).fetchall())

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def close(self) -> None:
        self.conn.close()
