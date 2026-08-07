"""Build the checked-in v2-format fixture DB (tests/fixtures/v2_format.db).

This reproduces the schema **as it existed at user_version=2** — the 0.2.x
sleep-time-maintenance layout, BEFORE the schema-v3 entity-collation migration
(`fact.record_kind` and the maintenance tables ARE present; `entity.name_key` and
`ix_entity_key` are NOT). Like make_v1_fixture.py it does NOT import the current
SqliteStore, whose _init_schema would immediately migrate the file to v3 and
defeat the purpose; it lays the v2 DDL down by hand.

The fixture deliberately carries a **pre-existing case-split entity pair**
('Acme' written earlier, 'ACME' written later, one fact each) — exactly the
damage v3 is a forward-fix for. The migration test asserts that both rows and
both facts survive the upgrade untouched (the migration backfills `name_key`; it
never heals a split, because re-pointing `fact.subject_id` is a new mutation verb
and its own decision) and that the next mention resolves to the OLDEST row.

Determinism: fixed ids, fixed timestamps, fixed tiny vectors — so re-running the
script byte-reproduces the same DB. Run from the repo root:

    .venv/bin/python tests/fixtures/make_v2_fixture.py

The migration regression tests (tests/test_schema_migration.py) open the result
with the CURRENT code and assert a clean, once-only 2→3 upgrade.
"""

from __future__ import annotations

import struct
from pathlib import Path

import sqlite_vec

# ── v2 SCHEMA (verbatim from the store/schema.py layout at user_version=2, plus
#    the record_kind column the v2 branch ALTERs in; entity has NO name_key) ──
DIM = 8
COARSE_DIM = 4

V2_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS episode (
  id          TEXT PRIMARY KEY,
  namespace   TEXT NOT NULL,
  raw         TEXT NOT NULL,
  source      TEXT,
  t_ref       INTEGER NOT NULL,
  created_at  INTEGER NOT NULL
);

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
  created_at      INTEGER NOT NULL,
  -- the v2 ALTER, materialized here because this file IS a v2 file
  record_kind     TEXT NOT NULL DEFAULT 'fact'
);
CREATE INDEX IF NOT EXISTS ix_fact_ns_latest ON fact(namespace, is_latest);
CREATE INDEX IF NOT EXISTS ix_fact_slot      ON fact(namespace, subject_id, predicate);
CREATE INDEX IF NOT EXISTS ix_fact_valid     ON fact(namespace, valid_at, valid_to);

CREATE VIRTUAL TABLE IF NOT EXISTS fact_vec USING vec0(
  fact_id        TEXT PRIMARY KEY,
  is_latest      INTEGER,
  tier           TEXT,
  namespace      TEXT,
  embedding      FLOAT[{DIM}],
  embedding_256  FLOAT[{COARSE_DIM}]
);

CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5(
  fact_id UNINDEXED,
  fact_text
);

-- v2 maintenance layer
CREATE TABLE IF NOT EXISTS fact_derivation (
  summary_id TEXT NOT NULL REFERENCES fact(id),
  source_id  TEXT NOT NULL REFERENCES fact(id),
  run_id     TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (summary_id, source_id)
);
CREATE INDEX IF NOT EXISTS ix_derivation_source ON fact_derivation(source_id);

CREATE TABLE IF NOT EXISTS maintenance_run (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  started_at INTEGER NOT NULL, finished_at INTEGER,
  heartbeat_at INTEGER,
  trigger TEXT NOT NULL,
  cursor_id TEXT,
  config_hash TEXT, stats_json TEXT,
  status TEXT NOT NULL DEFAULT 'running'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_live
  ON maintenance_run(namespace) WHERE status='running';

CREATE TABLE IF NOT EXISTS maintenance_proposal (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES maintenance_run(id),
  namespace TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  expiry_reason TEXT,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  decided_at INTEGER, decided_by TEXT,
  applied_at INTEGER, edited_text TEXT,
  evidence_backend TEXT
);
"""

FIXTURE_PATH = Path(__file__).with_name("v2_format.db")

NS = "v2user"
EP_ID = "0000ep000000-0000000000000001"

T0 = 1_700_000_000_000

# 'user' plus the case-split pair the v3 migration must NOT heal. Written in the
# order a real store would have: 'Acme' first, 'ACME' later.
ENTITIES = [
    ("0000en000000-0000000000000001", "user", "person", T0),
    ("0000en000000-0000000000000002", "Acme", None, T0 + 1_000),
    ("0000en000000-0000000000000003", "ACME", None, T0 + 2_000),
]

FACTS = [
    {
        "id": "0000fa000000-0000000000000001",
        "subject_id": "0000en000000-0000000000000001",
        "predicate": "works_at",
        "object_literal": "Acme",
        "fact_text": "The user works at Acme.",
        "valid_at": T0,
        "is_latest": 1,
    },
    {
        "id": "0000fa000000-0000000000000002",
        "subject_id": "0000en000000-0000000000000002",
        "predicate": "uses",
        "object_literal": "Postgres",
        "fact_text": "Acme uses Postgres.",
        "valid_at": T0 + 1_000,
        "is_latest": 1,
    },
    {
        # The split's damage, frozen into the fixture: one company, two identities.
        "id": "0000fa000000-0000000000000003",
        "subject_id": "0000en000000-0000000000000003",
        "predicate": "uses",
        "object_literal": "Redis",
        "fact_text": "ACME uses Redis.",
        "valid_at": T0 + 2_000,
        "is_latest": 1,
    },
]


def _vec(seed: int, dim: int) -> bytes:
    """A fixed, L2-normalized float32 vector — vec0's float32 wire format."""
    raw = [float((seed * (i + 1)) % 7 + 1) for i in range(dim)]
    norm = sum(x * x for x in raw) ** 0.5
    unit = [x / norm for x in raw]
    return struct.pack(f"{dim}f", *unit)


def build(path: Path = FIXTURE_PATH) -> Path:
    import sqlite3

    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(V2_SCHEMA)

    db.execute(
        "INSERT INTO episode(id, namespace, raw, source, t_ref, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (EP_ID, NS, "seed episode", "user", T0, T0),
    )
    for eid, name, etype, created in ENTITIES:
        db.execute(
            "INSERT INTO entity(id, namespace, name, type, summary, resolved_id, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, NS, name, etype, None, None, created),
        )

    for i, f in enumerate(FACTS, start=1):
        # v2 fact column list — WITH record_kind, WITHOUT anything v3 adds.
        db.execute(
            """INSERT INTO fact(
                 id, namespace, subject_id, predicate, object_id, object_literal, fact_text,
                 valid_at, valid_to, superseded_by, is_latest,
                 ingested_at, expired_at, invalidated_by,
                 confidence, salience, last_access, access_count, is_inference, tier,
                 record_kind, episode_id, created_at)
               VALUES (?,?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?,?)""",
            (f["id"], NS, f["subject_id"], f["predicate"], None, f["object_literal"],
             f["fact_text"],
             f["valid_at"], None, None, f["is_latest"],
             f["valid_at"], None, None,
             1.0, 1.0, None, 0, 0, "hot",
             "fact", EP_ID, f["valid_at"]),
        )
        db.execute(
            "INSERT INTO fact_vec(fact_id, namespace, is_latest, tier, embedding, embedding_256) "
            "VALUES (?,?,?,?,?,?)",
            (f["id"], NS, f["is_latest"], "hot", _vec(i, DIM), _vec(i, COARSE_DIM)),
        )
        db.execute(
            "INSERT INTO fact_fts(fact_id, fact_text) VALUES (?,?)",
            (f["id"], f["fact_text"]),
        )

    # The v2 stamp — this is what makes the file "v2-format" for the migration.
    db.execute("PRAGMA user_version = 2")
    db.commit()
    db.close()
    return path


if __name__ == "__main__":
    out = build()
    import sqlite3

    check = sqlite3.connect(out)
    version = check.execute("PRAGMA user_version").fetchone()[0]
    fact_cols = [r[1] for r in check.execute("PRAGMA table_info(fact)").fetchall()]
    ent_cols = [r[1] for r in check.execute("PRAGMA table_info(entity)").fetchall()]
    tables = [
        r[0]
        for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    check.close()
    assert version == 2, version
    assert "record_kind" in fact_cols, "fixture must be v2-format (record_kind present)"
    assert "name_key" not in ent_cols, "fixture must be PRE-v3 (no entity.name_key)"
    assert "maintenance_run" in tables, "fixture must be v2-format (maintenance tables)"
    print(f"built {out} — user_version={version}, entity cols={ent_cols}, tables={tables}")
