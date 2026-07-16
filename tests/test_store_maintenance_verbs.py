"""Task 1 — store maintenance plumbing: busy_timeout, batch(), new verbs.

All offline (FakeEmbedder), against a real SqliteStore. These pin the store-level
foundation the sleep-time maintenance job stands on (design spec §4.0, §7.1):
  - busy_timeout PRAGMA is applied on connect
  - batch() is an atomic unit-of-work: one COMMIT at exit, ROLLBACK on exception
  - retire_duplicate / set_tier keep the fact and fact_vec surfaces in sync
  - get_embedding round-trips the stored float32 vector (no re-embed)
  - the two cursor iterators find the right rows, incl. the verified cursor gap
  - the retire_duplicate chain invariant survives a transitive B→A→D retirement
"""

from __future__ import annotations

import numpy as np
import pytest

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact


@pytest.fixture
def store(tmp_path):
    emb = FakeEmbedder()
    s = SqliteStore(tmp_path / "ns.db", dim=emb.dim, coarse_dim=emb.coarse_dim)
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    s.add_episode(ep)
    ent = s.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    s._emb = emb
    s._ep = ep
    s._ent = ent
    yield s
    s.close()


def _add_fact(store, text, *, valid_at, is_latest=1, valid_to=None, tier="hot"):
    f = Fact(
        namespace="ns", subject_id=store._ent.id, predicate="works_at",
        fact_text=text, valid_at=valid_at, episode_id=store._ep.id,
        is_latest=is_latest, valid_to=valid_to, tier=tier,
    )
    full, coarse = store._emb.embed_with_coarse(text)
    store.add_fact(f, full, coarse)
    return f


# ── Step 1: busy_timeout ──
def test_busy_timeout_default_applied(tmp_path):
    s = SqliteStore(tmp_path / "d.db")
    assert s._db.execute("PRAGMA busy_timeout").fetchone()[0] == 1500
    s.close()


def test_busy_timeout_override(tmp_path):
    s = SqliteStore(tmp_path / "m.db", busy_timeout_ms=5000)
    assert s._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    s.close()


# ── Step 2: batch() atomicity ──
def test_batch_single_commit_visible(store):
    with store.batch():
        _add_fact(store, "user works at acme", valid_at=1_000)
        _add_fact(store, "user works at globex", valid_at=2_000)
    n = store._db.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    assert n == 2, "both rows committed once at batch exit"


def test_batch_rolls_back_on_exception(store):
    _add_fact(store, "user works at acme", valid_at=1_000)  # committed pre-batch
    with pytest.raises(RuntimeError):
        with store.batch():
            _add_fact(store, "user works at globex", valid_at=2_000)
            raise RuntimeError("boom mid-batch")
    rows = store._db.execute("SELECT fact_text FROM fact").fetchall()
    texts = [r["fact_text"] for r in rows]
    assert texts == ["user works at acme"], "mid-batch work rolled back, pre-batch survives"


def test_batch_not_reentrant(store):
    with pytest.raises(RuntimeError):
        with store.batch():
            with store.batch():
                pass
    # the connection recovered — a fresh batch still works
    with store.batch():
        _add_fact(store, "user works at acme", valid_at=1_000)
    assert store._db.execute("SELECT COUNT(*) FROM fact").fetchone()[0] == 1


def test_batch_suppresses_per_call_commit(store):
    """Inside a batch, a mutator's own commit() is suppressed — the row is not yet
    durable to a second connection until the batch's single COMMIT."""
    with store.batch():
        _add_fact(store, "user works at acme", valid_at=1_000)
        other = SqliteStore(store.path)
        try:
            seen = other._db.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
        finally:
            other.close()
        assert seen == 0, "uncommitted batch write invisible to another connection"
    other = SqliteStore(store.path)
    try:
        seen = other._db.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    finally:
        other.close()
    assert seen == 1, "visible after the batch COMMIT"


# ── Step 3: retire_duplicate two-surface sync ──
def test_retire_duplicate_two_surface(store):
    survivor = _add_fact(store, "user works at acme", valid_at=1_000)
    loser = _add_fact(store, "user works at acme", valid_at=2_000)
    store.retire_duplicate(loser.id, survivor.id)

    row = store._db.execute(
        "SELECT is_latest, superseded_by, valid_to FROM fact WHERE id=?", (loser.id,)
    ).fetchone()
    assert row["is_latest"] == 0
    assert row["superseded_by"] == survivor.id
    assert row["valid_to"] is None, "valid_to UNTOUCHED (verb (c) as-of-safe)"

    vec = store._db.execute(
        "SELECT is_latest FROM fact_vec WHERE fact_id=?", (loser.id,)
    ).fetchone()
    assert vec["is_latest"] == 0, "fact_vec mirrored — loser drops out of latest search"

    surv = store._db.execute(
        "SELECT is_latest FROM fact WHERE id=?", (survivor.id,)
    ).fetchone()
    assert surv["is_latest"] == 1, "survivor stays latest"


def test_retire_duplicate_survivor_resolved_to_canonical(store):
    """(i) If the survivor arg is itself retired, resolve to its live canonical."""
    canonical = _add_fact(store, "user works at acme", valid_at=1_000)
    mid = _add_fact(store, "user works at acme", valid_at=2_000)
    store.retire_duplicate(mid.id, canonical.id)  # mid → canonical
    loser = _add_fact(store, "user works at acme", valid_at=3_000)
    store.retire_duplicate(loser.id, mid.id)  # arg is mid (retired) → must resolve to canonical

    row = store._db.execute(
        "SELECT superseded_by FROM fact WHERE id=?", (loser.id,)
    ).fetchone()
    assert row["superseded_by"] == canonical.id, "survivor resolved to live canonical (depth 1)"


def test_retire_duplicate_chain_invariant_transitive(store):
    """(ii) rev-3 blocker: retire B→A, then A→D re-points B to D. After it, ZERO open
    retired duplicates point at a non-latest row."""
    A = _add_fact(store, "user works at acme", valid_at=1_000)
    B = _add_fact(store, "user works at acme", valid_at=2_000)
    D = _add_fact(store, "user works at acme", valid_at=3_000)

    store.retire_duplicate(B.id, A.id)   # B → A
    store.retire_duplicate(A.id, D.id)   # A → D ; must re-point B → D too

    b = store._db.execute("SELECT superseded_by FROM fact WHERE id=?", (B.id,)).fetchone()
    a = store._db.execute("SELECT superseded_by FROM fact WHERE id=?", (A.id,)).fetchone()
    assert a["superseded_by"] == D.id
    assert b["superseded_by"] == D.id, "B re-pointed to the live canonical D"

    # Standing invariant: no open retired duplicate points at a non-latest row.
    orphans = store._db.execute(
        """SELECT f.id FROM fact f
           JOIN fact t ON t.id = f.superseded_by
           WHERE f.superseded_by IS NOT NULL AND f.valid_to IS NULL AND t.is_latest = 0"""
    ).fetchall()
    assert orphans == [], "zero open duplicates pointing at a non-latest row"


def test_retire_duplicate_survivor_equals_loser_is_rejected(store):
    f = _add_fact(store, "user works at acme", valid_at=1_000)
    with pytest.raises(ValueError):
        store.retire_duplicate(f.id, f.id)
    row = store._db.execute("SELECT is_latest FROM fact WHERE id=?", (f.id,)).fetchone()
    assert row["is_latest"] == 1, "no-op on the store — the fact stays latest"


# ── Step 3: set_tier two-surface sync ──
def test_set_tier_two_surface(store):
    f = _add_fact(store, "user works at acme", valid_at=1_000, tier="hot")
    store.set_tier(f.id, "cold")
    frow = store._db.execute("SELECT tier FROM fact WHERE id=?", (f.id,)).fetchone()
    vrow = store._db.execute("SELECT tier FROM fact_vec WHERE fact_id=?", (f.id,)).fetchone()
    assert frow["tier"] == "cold"
    assert vrow["tier"] == "cold", "fact_vec tier metadata mirrored"


# ── Step 3: get_embedding round-trip ──
def test_get_embedding_roundtrip(store):
    f = _add_fact(store, "user works at acme", valid_at=1_000)
    full, _ = store._emb.embed_with_coarse("user works at acme")
    got = store.get_embedding(f.id)
    assert got is not None
    assert got.dtype == np.float32
    assert np.array_equal(got, full.astype(np.float32)), "exact float32 round-trip, no re-embed"


def test_get_embedding_missing_returns_none(store):
    assert store.get_embedding("nope-does-not-exist") is None


# ── Step 3: iterators ──
def test_iter_latest_facts_high_water(store):
    f1 = _add_fact(store, "user works at acme", valid_at=1_000)
    f2 = _add_fact(store, "user works at globex", valid_at=2_000)
    store.supersede_fact(f1.id, f2.id, valid_to=2_000)  # f1 no longer latest
    f3 = _add_fact(store, "user works at zorbex", valid_at=3_000)

    all_latest = [f.id for f in store.iter_latest_facts()]
    assert f1.id not in all_latest, "superseded fact excluded (is_latest=0)"
    assert set(all_latest) == {f2.id, f3.id}
    # yielded ascending by id (the scan's high-water order)
    assert all_latest == sorted(all_latest)

    # The scan is a strict id high-water cut. Ids are time-sortable but the same-ms
    # tiebreak is random, so key off the actual max id, not insertion order.
    latest_ids = sorted({f2.id, f3.id})
    after = [f.id for f in store.iter_latest_facts(after_id=latest_ids[0])]
    assert after == [latest_ids[1]], "returns exactly the is_latest ids strictly greater"
    assert list(store.iter_latest_facts(after_id=latest_ids[1])) == [], "cursor at max → empty"


def _add_with_id(store, fact_id, text, predicate, *, valid_at):
    f = Fact(
        namespace="ns", subject_id=store._ent.id, predicate=predicate,
        fact_text=text, valid_at=valid_at, episode_id=store._ep.id, id=fact_id,
    )
    full, coarse = store._emb.embed_with_coarse(text)
    store.add_fact(f, full, coarse)
    return f


def test_iter_slots_touched_since_finds_quiet_slot_duplicate(store):
    """The verified cursor gap: a duplicate landing on a long-quiet slot must be
    surfaced by joining new-facts-since-cursor → DISTINCT slots.

    Explicit monotonic ids so the cursor deterministically precedes the later inserts
    (new_id()'s same-ms tiebreak is random — see test_iter_latest_facts_high_water)."""
    # Slot `works_at` gets an early fact; the cursor is taken just after it.
    early = _add_with_id(store, "0001-early", "user works at acme", "works_at", valid_at=1_000)
    cursor = early.id  # "0001-early"

    # A different, busy predicate churns; then MUCH later a duplicate lands on the
    # original quiet slot (same subject+predicate as `early`).
    _add_with_id(store, "0002-hobby", "user likes jazz", "likes", valid_at=2_000)
    late_dup = _add_with_id(store, "0003-dup", "user works at acme", "works_at", valid_at=9_000)
    assert late_dup.id > cursor

    slots = set(store.iter_slots_touched_since(cursor))
    assert (store._ent.id, "works_at") in slots, "quiet works_at slot resurfaced by its new dup"
    assert (store._ent.id, "likes") in slots, "the busy slot is included too"

    # A cursor at/after every fact yields nothing (no slot gained a member since).
    assert set(store.iter_slots_touched_since(late_dup.id)) == set()
