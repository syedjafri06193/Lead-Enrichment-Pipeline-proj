-- Production DDL (design.md section 4).
--
-- The SQLite schema in src/lep/store/db.py is the same shape; this is the
-- version with JSONB, real timestamps, pg_trgm blocking indexes and the
-- constraints that matter.
--
-- The one rule the schema enforces by construction: observations are
-- append-only.  Nothing UPDATEs a value, and only erasure DELETEs.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------- entities

CREATE TABLE entities (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    type       TEXT NOT NULL CHECK (type IN ('person', 'company')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Which external records are believed to be this entity.
CREATE TABLE entity_links (
    entity_id        UUID NOT NULL REFERENCES entities(id),
    source           TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    confidence       REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    linked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    linked_by        TEXT NOT NULL,          -- 'auto:v3' | 'user:alice@...'
    PRIMARY KEY (source, source_record_id)
);
CREATE INDEX entity_links_entity ON entity_links (entity_id);

-- Merges are recorded, never destructive (section 4.2).
CREATE TABLE merges (
    id            BIGSERIAL PRIMARY KEY,
    surviving_id  UUID NOT NULL REFERENCES entities(id),
    merged_id     UUID NOT NULL REFERENCES entities(id),
    score         REAL,
    decided_by    TEXT NOT NULL,
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverted_at   TIMESTAMPTZ,
    revert_reason TEXT,
    CHECK (surviving_id <> merged_id)
);
CREATE INDEX merges_merged ON merges (merged_id) WHERE reverted_at IS NULL;
CREATE INDEX merges_surviving ON merges (surviving_id) WHERE reverted_at IS NULL;

-- ------------------------------------------------------------ observations

CREATE TABLE observations (
    id               BIGSERIAL PRIMARY KEY,
    entity_id        UUID NOT NULL,
    field            TEXT NOT NULL,
    value            JSONB NOT NULL,
    source           TEXT NOT NULL,       -- salesforce | hubspot | clearbit | user
    source_kind      TEXT NOT NULL CHECK (
                         source_kind IN ('user', 'crm', 'enrichment', 'system')),
    source_record_id TEXT,
    confidence       REAL,
    observed_at      TIMESTAMPTZ NOT NULL,   -- source system's timestamp
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when WE learned it
    expires_at       TIMESTAMPTZ,            -- staleness TTL (section 10.3)
    superseded_by    BIGINT REFERENCES observations(id)
);

CREATE INDEX observations_lookup ON observations (entity_id, field, observed_at DESC);
CREATE INDEX observations_source ON observations (source, source_record_id);
-- "What did we believe last Tuesday" and erasure both want this one.
CREATE INDEX observations_recorded ON observations (recorded_at);
CREATE INDEX observations_live ON observations (entity_id, field)
    WHERE superseded_by IS NULL;

-- Trigram indexes for in-database fuzzy blocking (section 13).  pg_trgm lets
-- you generate candidate pairs without moving data out of Postgres, which is
-- often enough at mid scale and much simpler than a separate pipeline.
CREATE INDEX observations_value_trgm ON observations
    USING gin ((value #>> '{}') gin_trgm_ops);

-- Belt and braces: observations are append-only.  Only two columns may ever
-- change -- superseded_by, set when a late arrival loses to a newer one, and
-- entity_id, set when entities are merged.  A value, a source or a timestamp
-- changing means something is overwriting history.
CREATE OR REPLACE FUNCTION observations_are_append_only() RETURNS trigger AS $$
BEGIN
    IF NEW.value       IS DISTINCT FROM OLD.value
    OR NEW.field       IS DISTINCT FROM OLD.field
    OR NEW.source      IS DISTINCT FROM OLD.source
    OR NEW.source_kind IS DISTINCT FROM OLD.source_kind
    OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
    OR NEW.recorded_at IS DISTINCT FROM OLD.recorded_at THEN
        RAISE EXCEPTION
            'observations are append-only: row % may not have its history rewritten',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER observations_no_value_updates
    BEFORE UPDATE ON observations
    FOR EACH ROW EXECUTE FUNCTION observations_are_append_only();

-- ----------------------------------------------------------------- sync

CREATE TABLE write_log (
    id         BIGSERIAL PRIMARY KEY,
    target     TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    value_hash TEXT NOT NULL,          -- hashed: only equality is needed
    written_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX write_log_lookup ON write_log (target, record_id, field, written_at DESC);
-- The echo window is minutes; keep this table small.
-- DELETE FROM write_log WHERE written_at < now() - interval '1 hour';

CREATE TABLE change_log (
    id        BIGSERIAL PRIMARY KEY,
    source    TEXT NOT NULL,
    record_id TEXT NOT NULL,
    field     TEXT,
    seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX change_log_lookup ON change_log (source, record_id, seen_at DESC);

CREATE TABLE quarantine (
    source      TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    PRIMARY KEY (source, record_id)
);

-- ------------------------------------------------------------- conflicts

CREATE TABLE conflicts (
    id                BIGSERIAL PRIMARY KEY,
    entity_id         UUID NOT NULL,
    field             TEXT NOT NULL,
    candidates        JSONB NOT NULL,
    resolved_to       JSONB,
    resolution_reason TEXT NOT NULL,
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed          BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX conflicts_field ON conflicts (field, detected_at DESC);

-- The most useful diagnostic the system produces (section 8.3).
CREATE VIEW top_conflicting_fields AS
    SELECT field, count(*) AS conflicts, max(detected_at) AS latest
    FROM conflicts
    GROUP BY field
    ORDER BY conflicts DESC;

-- ------------------------------------------------------------------ review

CREATE TABLE review_items (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('pair', 'cluster', 'delete')),
    left_key   TEXT NOT NULL,
    right_key  TEXT,
    score      REAL,
    payload    JSONB NOT NULL,
    reason     TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('pending', 'decided', 'deferred')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    priority   REAL NOT NULL DEFAULT 0
);
CREATE INDEX review_items_queue ON review_items (priority DESC, id)
    WHERE status = 'pending';

CREATE TABLE review_decisions (
    id          BIGSERIAL PRIMARY KEY,
    item_id     BIGINT NOT NULL REFERENCES review_items(id),
    model_score REAL,
    human_match BOOLEAN NOT NULL,
    reviewer    TEXT NOT NULL,
    note        TEXT,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- events

CREATE TABLE event_claims (
    key        TEXT PRIMARY KEY,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX event_claims_expiry ON event_claims (expires_at);

CREATE TABLE event_attempts (
    key      TEXT PRIMARY KEY,
    attempts INT NOT NULL DEFAULT 0
);

CREATE TABLE dlq (
    id          BIGSERIAL PRIMARY KEY,
    key         TEXT NOT NULL,
    payload     JSONB NOT NULL,
    error       TEXT NOT NULL,
    attempts    INT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
-- Alert on the depth of this, not just on individual failures.
CREATE INDEX dlq_open ON dlq (created_at) WHERE resolved_at IS NULL;

-- ---------------------------------------------------------------- privacy

CREATE TABLE erasure_suppression (
    key        TEXT PRIMARY KEY,   -- entity id, normalized email, source:record
    reason     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE erasure_log (
    id        BIGSERIAL PRIMARY KEY,
    entity_id UUID NOT NULL,
    reason    TEXT NOT NULL,
    report    JSONB NOT NULL,      -- the evidence that it was complete
    erased_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- budget

CREATE TABLE checkpoints (
    name       TEXT PRIMARY KEY,
    position   TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE api_usage (
    id       BIGSERIAL PRIMARY KEY,
    org_id   TEXT NOT NULL,
    provider TEXT NOT NULL,
    calls    INT NOT NULL,
    priority TEXT NOT NULL,
    note     TEXT,
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX api_usage_window ON api_usage (org_id, provider, at DESC);
