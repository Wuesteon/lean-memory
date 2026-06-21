"""SQLite + vec0 + FTS5 schema for Phase 0.

This is the spec's data model translated from its Postgres-flavored DDL to SQLite:
  - Postgres ENUM / halfvec / pgvector → SQLite TEXT + vec0 INT8[N].
  - `{dim}`/`{coarse_dim}` are filled at connect time from the embedder.
The monotemporal spine is always present; bi-temporal audit columns exist but are
only diverged from ingest time when the audit extra is enabled (deferred past Phase 0).
"""

SCHEMA_SQL = """
-- ── PROVENANCE LAYER ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS episode (
  id          TEXT PRIMARY KEY,
  namespace   TEXT NOT NULL,
  raw         TEXT NOT NULL,
  source      TEXT,
  t_ref       INTEGER NOT NULL,
  created_at  INTEGER NOT NULL
);

-- ── ENTITY LAYER ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entity (
  id          TEXT PRIMARY KEY,
  namespace   TEXT NOT NULL,
  name        TEXT NOT NULL,
  type        TEXT,
  summary     TEXT,
  resolved_id TEXT,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entity_lookup ON entity(namespace, name, type);

-- ── FACT LAYER (monotemporal spine always on; audit axis opt-in) ─────
CREATE TABLE IF NOT EXISTS fact (
  id              TEXT PRIMARY KEY,
  namespace       TEXT NOT NULL,
  subject_id      TEXT NOT NULL REFERENCES entity(id),
  predicate       TEXT NOT NULL,
  object_id       TEXT REFERENCES entity(id),
  object_literal  TEXT,
  fact_text       TEXT NOT NULL,

  valid_at        INTEGER NOT NULL,
  valid_to        INTEGER,
  superseded_by   TEXT REFERENCES fact(id),
  is_latest       INTEGER NOT NULL DEFAULT 1,

  ingested_at     INTEGER NOT NULL,
  expired_at      INTEGER,
  invalidated_by  TEXT REFERENCES fact(id),

  confidence      REAL NOT NULL DEFAULT 1.0,
  salience        REAL NOT NULL DEFAULT 0.0,
  last_access     INTEGER,
  access_count    INTEGER NOT NULL DEFAULT 0,
  is_inference    INTEGER NOT NULL DEFAULT 0,
  tier            TEXT NOT NULL DEFAULT 'hot',
  episode_id      TEXT NOT NULL REFERENCES episode(id),
  created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fact_ns_latest ON fact(namespace, is_latest);
CREATE INDEX IF NOT EXISTS ix_fact_slot      ON fact(namespace, subject_id, predicate);
CREATE INDEX IF NOT EXISTS ix_fact_valid     ON fact(namespace, valid_at, valid_to);

-- ── VECTOR INDEX (sqlite-vec vec0) ──────────────────────────────────
-- full 768 + 256-dim coarse Matryoshka vector. Stored FLOAT32 in Phase 0:
-- the spec targets int8 (size win, ~0.2pt quality cost per BET 1) but
-- sqlite-vec 0.1.9's int8 INSERT path is broken; flip to INT8[N] once fixed.
CREATE VIRTUAL TABLE IF NOT EXISTS fact_vec USING vec0(
  fact_id        TEXT PRIMARY KEY,
  is_latest      INTEGER,
  tier           TEXT,
  namespace      TEXT,
  embedding      FLOAT[{dim}],
  embedding_256  FLOAT[{coarse_dim}]
);

-- ── LEXICAL INDEX (FTS5, external-content style holding its own text) ─
CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5(
  fact_id UNINDEXED,
  fact_text
);
"""
