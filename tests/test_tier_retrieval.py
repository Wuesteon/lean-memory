"""Tier-filter retrieval matrix (design spec §8, §10.6).

The tier filter drops cold (maintenance-demoted) facts from the DEFAULT hot search
surface ONLY:
  - default latest-mode: cold facts are absent unless include_cold=True;
  - as_of queries NEVER filter tier (historical reads see everything);
  - byte-identical behavior when every row is tier='hot' (regression pin);
  - dense and sparse arms agree on the tier flag (both read what set_tier writes).

All offline on FakeEmbedder — no model, no network.
"""

from __future__ import annotations

import pytest

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact

T0 = 1_000_000_000_000


@pytest.fixture
def store(tmp_path):
    emb = FakeEmbedder()
    s = SqliteStore(tmp_path / "tier.db", dim=emb.dim, coarse_dim=emb.coarse_dim)
    ep = Episode(namespace="ns", raw="seed", t_ref=T0)
    s.add_episode(ep)
    s._emb = emb
    s._ep = ep
    s._subj = s.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    yield s
    s.close()


def _add(store, text, *, valid_at=T0, predicate="about", tier="hot", is_latest=1):
    f = Fact(
        namespace="ns", subject_id=store._subj.id, predicate=predicate,
        fact_text=text, valid_at=valid_at, episode_id=store._ep.id,
        tier=tier, is_latest=is_latest, salience=5.0,
    )
    full, coarse = store._emb.embed_with_coarse(text)
    store.add_fact(f, full, coarse)
    return f


def _dense(store, query, **kw):
    q = store._emb.embed_one(query)
    from lean_memory.embed.base import matryoshka_truncate

    qc = matryoshka_truncate(q, store._emb.coarse_dim)
    return {fid for fid, _ in store.dense_search(qc, q, 20, **kw)}


def _sparse(store, query, **kw):
    return {fid for fid, _ in store.sparse_search(query, 20, **kw)}


# ── default surface drops cold; include_cold opts back in ──────────────────────
def test_cold_fact_absent_from_default_search_present_with_include_cold(store):
    hot = _add(store, "the sky is blue today")
    cold = _add(store, "the sky is blue today anciently", tier="cold")

    # Default latest-mode: cold is filtered from BOTH arms.
    assert cold.id not in _dense(store, "sky blue")
    assert cold.id not in _sparse(store, "sky blue")
    assert hot.id in _dense(store, "sky blue")
    assert hot.id in _sparse(store, "sky blue")

    # include_cold=True: cold reappears on both arms.
    assert cold.id in _dense(store, "sky blue", include_cold=True)
    assert cold.id in _sparse(store, "sky blue", include_cold=True)


def test_set_tier_then_search_round_trip(store):
    """set_tier writes both surfaces; a demoted fact then drops from the default arms
    and returns under include_cold — the two-surface sync (§8/§10.6)."""
    f = _add(store, "quantum widget calibration notes")
    assert f.id in _dense(store, "quantum widget")
    assert f.id in _sparse(store, "quantum widget")

    with store.batch():
        store.set_tier(f.id, "cold")

    assert f.id not in _dense(store, "quantum widget")
    assert f.id not in _sparse(store, "quantum widget")
    assert f.id in _dense(store, "quantum widget", include_cold=True)
    assert f.id in _sparse(store, "quantum widget", include_cold=True)


# ── as_of NEVER filters tier ───────────────────────────────────────────────────
def test_as_of_ignores_tier(store):
    """A cold fact valid at T is visible in an as_of=T search regardless of tier — the
    as_of surface never filters tier (§8)."""
    cold = _add(store, "historical cold record about widgets", valid_at=T0, tier="cold")
    T_after = T0 + 1

    assert cold.id in _dense(store, "historical widgets", as_of=T_after)
    assert cold.id in _sparse(store, "historical widgets", as_of=T_after)
    # include_cold is irrelevant on as_of (already unfiltered) — same result.
    assert cold.id in _dense(store, "historical widgets", as_of=T_after, include_cold=True)
    assert cold.id in _sparse(store, "historical widgets", as_of=T_after, include_cold=True)


# ── dense/sparse arm agreement on the tier flag ────────────────────────────────
def test_dense_sparse_arm_agreement_on_tier(store):
    """Both arms make the SAME tier decision for the same fact across the matrix."""
    f = _add(store, "arm agreement widget probe", tier="cold")
    T_after = T0 + 1

    # Default: both exclude.
    assert (f.id in _dense(store, "arm agreement widget")) == (
        f.id in _sparse(store, "arm agreement widget")
    ) == False
    # include_cold: both include.
    assert (f.id in _dense(store, "arm agreement widget", include_cold=True)) == (
        f.id in _sparse(store, "arm agreement widget", include_cold=True)
    ) == True
    # as_of: both include.
    assert (f.id in _dense(store, "arm agreement widget", as_of=T_after)) == (
        f.id in _sparse(store, "arm agreement widget", as_of=T_after)
    ) == True


# ── byte-identical default when nothing is cold (regression pin) ───────────────
def test_byte_identical_default_when_nothing_cold(store):
    """When every row is tier='hot', default search results are exactly what they were
    before the tier filter existed — the filter is a no-op (§8 regression pin)."""
    ids = [_add(store, f"hot fact number {i} about widgets").id for i in range(5)]

    dense_default = _dense(store, "widgets")
    sparse_default = _sparse(store, "widgets")
    # include_cold on an all-hot store returns the SAME set (nothing cold to add).
    assert _dense(store, "widgets", include_cold=True) == dense_default
    assert _sparse(store, "widgets", include_cold=True) == sparse_default
    # All the hot facts are present.
    assert set(ids) <= dense_default
    assert set(ids) <= sparse_default


# ── the full as_of × include_cold × tier grid (§10.6) ──────────────────────────
def test_asof_include_cold_tier_matrix(store):
    """The matrix: a cold fact valid at T_valid, queried across
    {default, include_cold, as_of} — expected presence per §8."""
    T_valid = T0
    T_query = T0 + 1
    cold = _add(store, "matrix cold widget entry", valid_at=T_valid, tier="cold")
    hot = _add(store, "matrix hot widget entry", valid_at=T_valid, tier="hot")

    # (default, cold) -> absent ; (default, hot) -> present
    d = _dense(store, "matrix widget")
    s = _sparse(store, "matrix widget")
    assert cold.id not in d and cold.id not in s
    assert hot.id in d and hot.id in s

    # (include_cold, cold) -> present ; (include_cold, hot) -> present
    d = _dense(store, "matrix widget", include_cold=True)
    s = _sparse(store, "matrix widget", include_cold=True)
    assert cold.id in d and cold.id in s
    assert hot.id in d and hot.id in s

    # (as_of, cold) -> present ; (as_of, hot) -> present (tier ignored)
    d = _dense(store, "matrix widget", as_of=T_query)
    s = _sparse(store, "matrix widget", as_of=T_query)
    assert cold.id in d and cold.id in s
    assert hot.id in d and hot.id in s
