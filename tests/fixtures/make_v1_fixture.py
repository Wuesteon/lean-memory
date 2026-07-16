"""Build the checked-in v1-format fixture DB (tests/fixtures/v1_format.db).

This reproduces the schema **as it existed at user_version=1** — the 0.1.x layout,
BEFORE the schema-v2 migration (no `fact.record_kind`, no maintenance tables). It
does NOT import the current SqliteStore, whose _init_schema would immediately
migrate the file to v2 and defeat the purpose. Instead it lays down the v1 DDL by
hand and inserts a few rows through plain INSERTs that mirror add_fact's v1 column
list.

Determinism: fixed ids, fixed timestamps, fixed tiny vectors — so re-running the
script byte-reproduces the same DB (modulo SQLite page layout, which is stable for
this fixed input). Run from the repo root:

    .venv/bin/python tests/fixtures/make_v1_fixture.py

The migration regression test (tests/test_schema_migration.py) opens the result
with the CURRENT code and asserts a clean, once-only 1→2 upgrade.
"""

from __future__ import annotations

import struct
from pathlib import Path

import sqlite_vec

# ── v1 SCHEMA (verbatim from the store/schema.py layout at user_version=1;
#    fact has NO record_kind column, and there are NO maintenance tables) ──
DIM = 8
COARSE_DIM = 4

V1_SCHEMA = f"""
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
  created_at      INTEGER NOT NULL
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
"""

FIXTURE_PATH = Path(__file__).with_name("v1_format.db")

NS = "v1user"
EP_ID = "0000ep000000-0000000000000001"
ENT_ID = "0000en000000-0000000000000001"

# Two facts in one (subject, predicate) slot: an older superseded row + the latest.
FACTS = [
    {
        "id": "0000fa000000-0000000000000001",
        "predicate": "works_at",
        "object_literal": "Acme",
        "fact_text": "The user works at Acme.",
        "valid_at": 1_700_000_000_000,
        "valid_to": 1_700_000_100_000,
        "superseded_by": "0000fa000000-0000000000000002",
        "is_latest": 0,
    },
    {
        "id": "0000fa000000-0000000000000002",
        "predicate": "works_at",
        "object_literal": "Globex",
        "fact_text": "The user works at Globex.",
        "valid_at": 1_700_000_100_000,
        "valid_to": None,
        "superseded_by": None,
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
    db.executescript(V1_SCHEMA)

    db.execute(
        "INSERT INTO episode(id, namespace, raw, source, t_ref, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (EP_ID, NS, "seed episode", "user", 1_700_000_000_000, 1_700_000_000_000),
    )
    db.execute(
        "INSERT INTO entity(id, namespace, name, type, summary, resolved_id, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (ENT_ID, NS, "user", "person", None, None, 1_700_000_000_000),
    )

    for i, f in enumerate(FACTS, start=1):
        # v1 fact column list — deliberately WITHOUT record_kind.
        db.execute(
            """INSERT INTO fact(
                 id, namespace, subject_id, predicate, object_id, object_literal, fact_text,
                 valid_at, valid_to, superseded_by, is_latest,
                 ingested_at, expired_at, invalidated_by,
                 confidence, salience, last_access, access_count, is_inference, tier,
                 episode_id, created_at)
               VALUES (?,?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?)""",
            (f["id"], NS, ENT_ID, f["predicate"], None, f["object_literal"],
             f["fact_text"],
             f["valid_at"], f["valid_to"], f["superseded_by"], f["is_latest"],
             f["valid_at"], None, None,
             1.0, 1.0, None, 0, 0, "hot",
             EP_ID, f["valid_at"]),
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

    # The v1 stamp — this is what makes the file "v1-format" for the migration.
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    return path


if __name__ == "__main__":
    out = build()
    import sqlite3

    check = sqlite3.connect(out)
    version = check.execute("PRAGMA user_version").fetchone()[0]
    cols = [r[1] for r in check.execute("PRAGMA table_info(fact)").fetchall()]
    tables = [
        r[0]
        for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    check.close()
    assert version == 1, version
    assert "record_kind" not in cols, "fixture must be v1-format (no record_kind)"
    assert "maintenance_run" not in tables, "fixture must be v1-format (no v2 tables)"
    print(f"built {out} — user_version={version}, fact cols={len(cols)}, tables={tables}")
