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
-- NOTE (schema v3): `name` is the FIRST-SEEN surface form, kept verbatim for
-- display. Identity resolves on `entity.name_key` (NFC + casefold + whitespace
-- collapse of `name`) via ix_entity_key. BOTH the name_key column and that index
-- live ONLY in the versioned `if user_version < 3:` branch of _init_schema and
-- must never appear here: this blob runs on EVERY open, so (a) an index over
-- name_key here would reference a column a pre-v3 file does not have, and (b)
-- declaring name_key in the table below would collide with the branch's ADD
-- COLUMN on every FRESH store ('duplicate column name: name_key' — a fresh DB is
-- stamped 1 and flows through the same branch). Same trap as fact.record_kind.
-- (Keep DDL keywords out of these comment lines: the console's engine-schema
-- tripwire, inspect_sql.compute_engine_schema_fingerprint, digests every line
-- containing one, so a prose mention flips its hash with the DDL unchanged.)
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

-- ── MAINTENANCE LAYER (schema v2; sleep-time job, design spec §5) ────
-- All IF-NOT-EXISTS so they belong in the always-run blob. The non-idempotent
-- v2 DDL (ALTER TABLE fact ADD COLUMN record_kind) lives ONLY in the versioned
-- `if user_version < 2:` branch of _init_schema — never here — because ADD
-- COLUMN raises 'duplicate column name' on reopen.
CREATE TABLE IF NOT EXISTS fact_derivation (
  summary_id TEXT NOT NULL REFERENCES fact(id),
  source_id  TEXT NOT NULL REFERENCES fact(id),
  run_id     TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (summary_id, source_id)
);
CREATE INDEX IF NOT EXISTS ix_derivation_source ON fact_derivation(source_id);
  -- the staleness cascade's lookup path (§4.3)

CREATE TABLE IF NOT EXISTS maintenance_run (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  started_at INTEGER NOT NULL, finished_at INTEGER,
  heartbeat_at INTEGER,
  trigger TEXT NOT NULL,                          -- 'cli'|'mcp'|'auto'|'console'
  cursor_id TEXT,
  config_hash TEXT, stats_json TEXT,
  status TEXT NOT NULL DEFAULT 'running'          -- 'running'|'ok'|'aborted'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_live
  ON maintenance_run(namespace) WHERE status='running';
  -- the INSERT is the atomic lease claim: a second runner gets a constraint
  -- error, not a silent second row (verified race gap in rev 1)

CREATE TABLE IF NOT EXISTS maintenance_proposal (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES maintenance_run(id),
  namespace TEXT NOT NULL,
  kind TEXT NOT NULL,                              -- 'dedup_near'|'summarize'|'evict'
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',          -- 'pending'|'approved'|'rejected'|'edited'|'expired'
  expiry_reason TEXT,                              -- 'timeout'|'stale_target'
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  decided_at INTEGER, decided_by TEXT,             -- 'console'|'mcp'|'expiry'
  applied_at INTEGER, edited_text TEXT,
  evidence_backend TEXT                            -- 'stub'|'ollama:<model>'|embedder id
);
"""
