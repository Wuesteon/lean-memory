"""Schema v2 migration + the ledger/proposal CRUD (design spec §5, §4.0).

The migration is the project's FIRST persisted-format change, so it carries a
checked-in v1-format fixture DB (tests/fixtures/v1_format.db, built by
make_v1_fixture.py) and pins the whole 1→2 upgrade end-to-end:

  - a genuine v1-format file opens, migrates ONCE (adds fact.record_kind + the
    maintenance tables), and REOPENS cleanly — the ALTER-idempotence trap, where a
    second open would raise 'duplicate column name' if the ADD COLUMN lived in the
    always-run schema blob instead of the versioned `< 2` branch;
  - after migration user_version == 2 and a search still round-trips;
  - the fresh-create path stamps 2 and reopens clean;
  - a DB stamped by a newer release is never downgraded.

The CRUD half pins pure row round-trips plus the ux_run_live partial-unique-index
race (a second live run for a namespace hits the constraint). No decide/apply
logic is exercised here — that is a later task.

All offline (no model download); the fixture's tiny 8-dim vectors are opened with
matching dims so _check_existing_dims passes.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "v1_format.db"
# The fixture was built with these tiny dims (make_v1_fixture.py); the store must
# open it with matching dims or _check_existing_dims refuses the vec0 mismatch.
FIXTURE_DIM = 8
FIXTURE_COARSE_DIM = 4


def _user_version(store: SqliteStore) -> int:
    return store._db.execute("PRAGMA user_version").fetchone()[0]


@pytest.fixture
def v1_db(tmp_path):
    """A writable copy of the checked-in v1-format fixture."""
    assert FIXTURE_SRC.exists(), (
        f"missing {FIXTURE_SRC} — rebuild it with "
        "`.venv/bin/python tests/fixtures/make_v1_fixture.py`"
    )
    dst = tmp_path / "v1user.db"
    shutil.copy(FIXTURE_SRC, dst)
    return dst


# ── Migration: the v1-format fixture ──
def test_v1_fixture_is_genuinely_v1(v1_db):
    """Guard the fixture itself: it must be a v1 file (version 1, no record_kind,
    no maintenance tables) — otherwise the migration test proves nothing."""
    db = sqlite3.connect(v1_db)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        cols = [r[1] for r in db.execute("PRAGMA table_info(fact)").fetchall()]
        assert "record_kind" not in cols
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "maintenance_run" not in tables
        assert "fact_derivation" not in tables
        assert "maintenance_proposal" not in tables
    finally:
        db.close()


def test_v1_migrates_once_to_v2(v1_db):
    store = SqliteStore(v1_db, dim=FIXTURE_DIM, coarse_dim=FIXTURE_COARSE_DIM)
    try:
        assert _user_version(store) == 2, "upgraded to schema v2"
        # record_kind added, default 'fact' backfilled on the existing rows.
        cols = [r[1] for r in store._db.execute("PRAGMA table_info(fact)").fetchall()]
        assert "record_kind" in cols
        kinds = {
            r[0] for r in store._db.execute("SELECT record_kind FROM fact").fetchall()
        }
        assert kinds == {"fact"}, "pre-existing v1 rows backfilled to record_kind='fact'"
        # v2 tables now exist.
        tables = {
            r[0]
            for r in store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"fact_derivation", "maintenance_run", "maintenance_proposal"} <= tables
    finally:
        store.close()


def test_v1_reopens_cleanly_after_migration(v1_db):
    """The ALTER-idempotence trap: a SECOND open must not raise 'duplicate column
    name'. This is the whole point of gating the ADD COLUMN behind `user_version < 2`
    instead of putting it in the always-run schema blob."""
    SqliteStore(v1_db, dim=FIXTURE_DIM, coarse_dim=FIXTURE_COARSE_DIM).close()

    # A real second open of the now-migrated file — must be clean.
    reopened = SqliteStore(v1_db, dim=FIXTURE_DIM, coarse_dim=FIXTURE_COARSE_DIM)
    try:
        assert _user_version(reopened) == 2
    finally:
        reopened.close()


def test_migrated_v1_search_roundtrips(v1_db):
    """After migration the old rows are still queryable — a dense search over the
    fixture's stored vectors returns the latest fact."""
    store = SqliteStore(v1_db, dim=FIXTURE_DIM, coarse_dim=FIXTURE_COARSE_DIM)
    try:
        # Read a stored vector back and search with it — must find its own row.
        latest = store._db.execute(
            "SELECT id FROM fact WHERE is_latest=1"
        ).fetchone()["id"]
        full = store.get_embedding(latest)
        assert full is not None
        coarse = store._db.execute(
            "SELECT embedding_256 FROM fact_vec WHERE fact_id=?", (latest,)
        ).fetchone()["embedding_256"]
        coarse_vec = np.frombuffer(coarse, dtype=np.float32)

        hits = store.dense_search(coarse_vec, full, k=3, is_latest_only=True)
        assert any(fid == latest for fid, _ in hits), "migrated fact is retrievable"

        # And it round-trips through the Fact dataclass with record_kind populated.
        fact = store.get_fact(latest)
        assert fact is not None
        assert fact.record_kind == "fact"
    finally:
        store.close()


# ── Migration: fresh-create + no-downgrade (the same versioned branch) ──
def test_fresh_create_stamps_v2_and_reopens_clean(tmp_path):
    path = tmp_path / "fresh.db"
    store = SqliteStore(path, dim=768)
    assert _user_version(store) == 2
    # record_kind present on a fresh DB too (same ALTER branch, fresh is version 1
    # at that point).
    cols = [r[1] for r in store._db.execute("PRAGMA table_info(fact)").fetchall()]
    assert "record_kind" in cols
    store.close()

    reopened = SqliteStore(path, dim=768)  # must not raise
    assert _user_version(reopened) == 2
    reopened.close()


def test_newer_version_db_not_downgraded(tmp_path):
    path = tmp_path / "future.db"
    store = SqliteStore(path, dim=768)
    store._db.execute("PRAGMA user_version = 5")  # a hypothetical v>2 release
    store._db.commit()
    store.close()

    reopened = SqliteStore(path, dim=768)
    assert _user_version(reopened) == 5, "a newer stamp is never lowered"
    reopened.close()


# ── Ledger/proposal CRUD (§4.0, §5) ──
@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "ns.db", dim=768)
    yield s
    s.close()


def test_run_lifecycle_roundtrip(store):
    run_id = store.create_run(
        namespace="ns", trigger="cli", started_at=1_000, config_hash="cfg-abc"
    )
    assert run_id

    live = store.get_live_run("ns")
    assert live is not None
    assert live["id"] == run_id
    assert live["status"] == "running"
    assert live["trigger"] == "cli"
    assert live["config_hash"] == "cfg-abc"
    assert live["started_at"] == 1_000
    assert live["heartbeat_at"] == 1_000

    store.heartbeat_run(run_id, at=2_000)
    assert store.get_live_run("ns")["heartbeat_at"] == 2_000

    store.finish_run(
        run_id, status="ok", finished_at=3_000,
        stats_json='{"deduped": 2}', cursor_id="cur-1",
    )
    # No live run once finished — the lease is released.
    assert store.get_live_run("ns") is None
    row = store._db.execute(
        "SELECT * FROM maintenance_run WHERE id=?", (run_id,)
    ).fetchone()
    assert row["status"] == "ok"
    assert row["finished_at"] == 3_000
    assert row["stats_json"] == '{"deduped": 2}'
    assert row["cursor_id"] == "cur-1"


def test_get_live_run_none_when_no_run(store):
    assert store.get_live_run("ns") is None


def test_ux_run_live_second_live_run_raises(store):
    """The lease: a second status='running' INSERT for the same namespace hits the
    partial-unique index ux_run_live, raising IntegrityError — not a silent second
    live row."""
    store.create_run(namespace="ns", trigger="cli", started_at=1_000, config_hash=None)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_run(namespace="ns", trigger="mcp", started_at=1_100, config_hash=None)


def test_ux_run_live_allows_new_run_after_finish(store):
    """Finishing the first run releases the lease so a second run can claim it."""
    r1 = store.create_run(namespace="ns", trigger="cli", started_at=1_000, config_hash=None)
    store.finish_run(r1, status="ok", finished_at=2_000, stats_json=None, cursor_id=None)
    r2 = store.create_run(namespace="ns", trigger="cli", started_at=3_000, config_hash=None)
    assert r2 != r1
    assert store.get_live_run("ns")["id"] == r2


def test_ux_run_live_is_per_namespace(store):
    """The lease is per-namespace — concurrent live runs in DIFFERENT namespaces are
    fine. (This store is one file, but the index keys on the namespace column.)"""
    store.create_run(namespace="ns", trigger="cli", started_at=1_000, config_hash=None)
    other = store.create_run(namespace="other", trigger="cli", started_at=1_000, config_hash=None)
    assert store.get_live_run("other")["id"] == other


def test_proposal_roundtrip(store):
    run_id = store.create_run(namespace="ns", trigger="cli", started_at=1_000, config_hash=None)
    pid = store.stage_proposal(
        run_id=run_id, namespace="ns", kind="summarize",
        payload_json='{"subject": "user"}', created_at=1_000, expires_at=99_000,
        evidence_backend="stub",
    )
    assert pid

    got = store.get_proposal(pid)
    assert got is not None
    assert got["id"] == pid
    assert got["run_id"] == run_id
    assert got["namespace"] == "ns"
    assert got["kind"] == "summarize"
    assert got["payload_json"] == '{"subject": "user"}'
    assert got["status"] == "pending"
    assert got["created_at"] == 1_000
    assert got["expires_at"] == 99_000
    assert got["evidence_backend"] == "stub"
    # Lifecycle columns default empty — no decide/apply has run.
    assert got["decided_at"] is None
    assert got["applied_at"] is None
    assert got["expiry_reason"] is None


def test_get_proposal_missing_returns_none(store):
    assert store.get_proposal("nope-does-not-exist") is None


def test_list_proposals_filters_and_orders(store):
    run_id = store.create_run(namespace="ns", trigger="cli", started_at=1_000, config_hash=None)
    p1 = store.stage_proposal(
        run_id=run_id, namespace="ns", kind="summarize",
        payload_json="{}", created_at=1_000, expires_at=99_000,
    )
    p2 = store.stage_proposal(
        run_id=run_id, namespace="ns", kind="dedup_near",
        payload_json="{}", created_at=2_000, expires_at=99_000,
    )
    p3 = store.stage_proposal(
        run_id=run_id, namespace="ns", kind="evict",
        payload_json="{}", created_at=3_000, expires_at=99_000,
    )
    # a proposal in a different namespace must never leak in
    store.stage_proposal(
        run_id=run_id, namespace="other", kind="evict",
        payload_json="{}", created_at=4_000, expires_at=99_000,
    )

    all_ns = store.list_proposals("ns")
    assert [p["id"] for p in all_ns] == [p3, p2, p1], "newest first"

    by_kind = store.list_proposals("ns", kind="dedup_near")
    assert [p["id"] for p in by_kind] == [p2]

    pending = store.list_proposals("ns", status="pending")
    assert {p["id"] for p in pending} == {p1, p2, p3}

    assert store.list_proposals("ns", status="approved") == []
    assert len(store.list_proposals("ns", limit=1)) == 1
    assert store.list_proposals("other")[0]["namespace"] == "other"
