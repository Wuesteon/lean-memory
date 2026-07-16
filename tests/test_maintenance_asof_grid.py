"""Task 4 — the as-of grid invariance test at the STORE predicate (design spec §10.1).

The headline invariance argument, executable and scoped to what runs at THIS task:
the AUTO transforms (dedup_exact, evict auto-band) plus staging (which must be a
ZERO spine delta). The propose-transforms' spine effects only exist after the
Task-6 apply path; Task 6 re-runs this grid post-apply (§10.1 rev-3 note).

Corpus: backfills (id-order != valid_at-order), a functional slot with a
supersession, and a multivalued slot with exact duplicates. We snapshot the id-set
satisfying the store-level visibility predicate
    valid_at <= T AND (valid_to IS NULL OR valid_to > T)
(the pure point-in-time surface, is_latest_only=False) over a T grid, run the auto
transforms + all staging, and assert:
  - identical sets for every T < t_m (verb (c) + append-only ⇒ predicate-invariant),
  - staging alone is a ZERO spine delta (full fact-table dump unchanged),
  - no inverted intervals post-run (valid_to > valid_at),
  - a ranking-delta pin (§10.8): DEDUP-EXACT's last_access merge keeps the deduped
    fact's latest-mode top-k rank.
"""

from __future__ import annotations

import pytest

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.maintain import (
    MaintenanceConfig,
    MS_PER_DAY,
    dedup_exact,
    dedup_near,
    evict_auto,
    evict_propose,
    summarize,
)
from lean_memory.maintain.summarize import ExtractiveStubSummarizer
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact

T_M = 2_000_000_000_000  # maintenance time (epoch ms)
DAY = MS_PER_DAY


@pytest.fixture
def store(tmp_path):
    emb = FakeEmbedder()
    s = SqliteStore(tmp_path / "grid.db", dim=emb.dim, coarse_dim=emb.coarse_dim)
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    s.add_episode(ep)
    s._emb = emb
    s._ep = ep
    s._subj = s.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    yield s
    s.close()


def _add(store, text, *, predicate, valid_at, valid_to=None, superseded_by=None,
         is_latest=1, salience=5.0, access_count=0, last_access=None,
         record_kind="fact", embed_text=None):
    f = Fact(
        namespace="ns", subject_id=store._subj.id, predicate=predicate,
        fact_text=text, valid_at=valid_at, valid_to=valid_to,
        superseded_by=superseded_by, is_latest=is_latest, episode_id=store._ep.id,
        salience=salience, access_count=access_count, last_access=last_access,
        record_kind=record_kind,
    )
    full, coarse = store._emb.embed_with_coarse(embed_text if embed_text is not None else text)
    store.add_fact(f, full, coarse)
    return f


def _visible_at(store, T):
    """The STORE-level visibility predicate id-set at world-time T (is_latest_only=False)."""
    rows = store._db.execute(
        "SELECT id FROM fact WHERE valid_at <= ? AND (valid_to IS NULL OR valid_to > ?)",
        (T, T),
    ).fetchall()
    return frozenset(r["id"] for r in rows)


def _fact_dump(store):
    return [
        tuple(r)
        for r in store._db.execute(
            "SELECT id, is_latest, valid_at, valid_to, superseded_by, tier, "
            "access_count, last_access, record_kind FROM fact ORDER BY id"
        ).fetchall()
    ]


def _build_corpus(store):
    """A corpus with backfills, a functional supersession, and exact dups on a
    multivalued slot. Returns a dict of the notable facts."""
    # Functional slot 'works_at': acme (valid_at t0) superseded by globex (valid_at t1).
    t0 = T_M - 500 * DAY
    t1 = T_M - 300 * DAY
    acme = _add(store, "user works at acme", predicate="works_at", valid_at=t0,
                valid_to=t1, superseded_by=None, is_latest=0)
    globex = _add(store, "user works at globex", predicate="works_at", valid_at=t1)
    store._db.execute("UPDATE fact SET superseded_by=? WHERE id=?", (globex.id, acme.id))
    store._db.commit()

    # Multivalued slot 'likes' with EXACT duplicates (case/whitespace variants) —
    # BACKFILLED so id-order != valid_at-order (the later-ingested row is older).
    jazz_a = _add(store, "likes jazz", predicate="likes", valid_at=T_M - 400 * DAY,
                  access_count=1, last_access=T_M - 350 * DAY)
    jazz_b = _add(store, "LIKES  jazz", predicate="likes", valid_at=T_M - 450 * DAY,
                  access_count=4, last_access=T_M - 10 * DAY)  # older world-time, newer access
    # A distinct multivalued value that must NEVER merge.
    blues = _add(store, "likes blues", predicate="likes", valid_at=T_M - 420 * DAY)

    # An auto-band eviction candidate (salience<2, access 0, age>180d) on its own slot.
    trivial = _add(store, "trivial ancient note", predicate="notes", valid_at=T_M - 300 * DAY,
                   salience=1.0, access_count=0)

    return {
        "acme": acme, "globex": globex,
        "jazz_a": jazz_a, "jazz_b": jazz_b, "blues": blues, "trivial": trivial,
    }


def test_asof_grid_invariant_under_autos_and_staging(store):
    facts = _build_corpus(store)
    cfg = MaintenanceConfig()

    grid = [T_M - t * DAY for t in (480, 460, 430, 410, 350, 250, 100, 1)]
    before_sets = {T: _visible_at(store, T) for T in grid}
    dump_before_staging = _fact_dump(store)

    # ── Phase 1: STAGE all proposals (must be ZERO spine delta) ──
    slots = [(store._subj.id, p) for p in ("works_at", "likes", "notes")]
    run_id = store.create_run("ns", "cli", T_M, cfg.config_hash())
    dedup_near(store, cfg, T_M, slots, run_id=run_id)
    summarize(store, cfg, T_M, ExtractiveStubSummarizer(), run_id=run_id)
    evict_propose(store, cfg, T_M, run_id=run_id)
    assert _fact_dump(store) == dump_before_staging, "staging wrote ZERO spine changes"

    # ── Phase 2: AUTO transforms (dedup_exact + evict auto-band) ──
    merges = dedup_exact(store, cfg, T_M, slots)
    demoted = evict_auto(store, cfg, T_M)

    # The exact jazz duplicates merged; blues never did.
    assert len(merges) == 1
    merged_ids = set(merges[0].loser_ids) | {merges[0].survivor_id}
    assert merged_ids == {facts["jazz_a"].id, facts["jazz_b"].id}
    assert facts["blues"].id not in merged_ids
    # The trivial note was auto-demoted.
    assert demoted == [facts["trivial"].id]

    # ── Invariance: the store predicate is bit-identical for every T < t_m ──
    for T in grid:
        assert T < T_M
        assert _visible_at(store, T) == before_sets[T], f"predicate changed at T={T}"

    # ── No inverted intervals anywhere post-run ──
    inverted = store._db.execute(
        "SELECT id FROM fact WHERE valid_to IS NOT NULL AND valid_to <= valid_at"
    ).fetchall()
    assert inverted == [], "no inverted (valid_to <= valid_at) intervals post-run"


def test_asof_grid_dedup_exact_leaves_valid_to_untouched(store):
    """DEDUP-EXACT is verb (c): the retired duplicate's valid_to stays NULL, so it
    stays visible on the pure as-of surface for every T after its valid_at."""
    facts = _build_corpus(store)
    cfg = MaintenanceConfig()
    slots = [(store._subj.id, "likes")]
    dedup_exact(store, cfg, T_M, slots)

    loser = facts["jazz_a"] if facts["jazz_a"].valid_at > facts["jazz_b"].valid_at else facts["jazz_b"]
    row = store._db.execute(
        "SELECT is_latest, valid_to FROM fact WHERE id=?", (loser.id,)
    ).fetchone()
    assert row["is_latest"] == 0  # dropped from the latest surface
    assert row["valid_to"] is None  # but as-of-visible: valid_to untouched
    # Visible at a T after its own valid_at (pure as-of surface).
    T_after = max(facts["jazz_a"].valid_at, facts["jazz_b"].valid_at) + 1
    assert loser.id in _visible_at(store, T_after)


def test_dedup_exact_ranking_delta_pin(store):
    """§10.8 ranking honesty: after DEDUP-EXACT the survivor keeps the deduped
    fact's RECENCY via the last_access merge, so it holds its latest-mode top-k
    rank. Constructed so that WITHOUT the merge the survivor would be de-ranked.

    We score the survivor's retriever recency term before vs after the merge and
    assert it rose to the cluster's freshest last_access (the merge rule), which is
    what keeps its rank."""
    from lean_memory.retrieve.retriever import DECAY_LAMBDA
    import math

    # Survivor = oldest valid_at, but STALE last_access; loser = newer restatement
    # with a FRESH last_access. Without the merge the survivor stays stale.
    survivor = _add(store, "user works at acme", predicate="works_at",
                    valid_at=T_M - 500 * DAY, access_count=1,
                    last_access=T_M - 490 * DAY)  # very stale
    loser = _add(store, "USER WORKS AT ACME", predicate="works_at",
                 valid_at=T_M - 400 * DAY, access_count=1,
                 last_access=T_M - 2 * DAY)  # fresh restatement

    def recency(last_access):
        age = max(0, T_M - last_access)
        return math.exp(-DECAY_LAMBDA * age)

    rec_before = recency(survivor.last_access)  # stale survivor recency

    dedup_exact(store, MaintenanceConfig(), T_M, [(store._subj.id, "works_at")])

    merged = store._db.execute(
        "SELECT access_count, last_access FROM fact WHERE id=?", (survivor.id,)
    ).fetchone()
    # last_access = max coalesce over cluster = loser's fresh last_access.
    assert merged["last_access"] == loser.last_access
    assert merged["access_count"] == 2  # 1 + 1 summed
    rec_after = recency(merged["last_access"])
    # The merge lifted the survivor's recency term toward "fresh" — it is NOT
    # de-ranked. Dropping the merge would have left it at the stale rec_before.
    assert rec_after > rec_before
    assert rec_after > 0.9  # ~2 days of decay → near-fresh
