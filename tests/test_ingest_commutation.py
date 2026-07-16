"""Task 3 — the two ingest hooks that restore ingest commutation (design spec
§3.1 condition 3, §4.0 duplicate-cascade, §4.3 staleness cascade).

Offline (FakeEmbedder/StubTyper), against a real Memory + SqliteStore. These pin
the two empirically-demonstrated rev-1 wrong answers the hooks fix (§14):

  - RESURRECTION (§10.2): a duplicate retired by maintenance (valid_to NULL) is
    invisible to find_latest_in_slot, so ordinary ingest supersession never
    closes it — after the survivor is superseded the retired duplicate resurfaces
    as a permanently-open interval on the pure as-of surface. The duplicate-cascade
    in supersede_fact closes it at the same world-time as the survivor.
  - TRANSITIVE resurrection (§10.2, the rev-3 killer): B→A→D retirement; when D is
    superseded, BOTH A and B must close at V (Task 1's chain invariant re-points B
    to D, so the single-level cascade reaches it).
  - STALE SUMMARY (§10.3): a summary derived from a source fact must leave the
    default surface the moment ordinary ingest contradicts the source. The
    staleness cascade flips the derived summary is_latest=0.

Plus the no-op pin (§10.10): on a maintenance-naive DB both cascades' triggering
sets are provably empty, so a full ingest+search sequence is unchanged.
"""

from __future__ import annotations

from lean_memory import Memory
from lean_memory.embed.fake import FakeEmbedder
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact


# ── low-level store fixture helpers (mirror test_store_maintenance_verbs.py) ──
def _fresh_store(tmp_path, name="ns.db"):
    emb = FakeEmbedder()
    s = SqliteStore(tmp_path / name, dim=emb.dim, coarse_dim=emb.coarse_dim)
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    s.add_episode(ep)
    ent = s.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    s._emb = emb
    s._ep = ep
    s._ent = ent
    return s


def _add_fact(store, text, *, valid_at, predicate="works_at", is_inference=0,
              record_kind="fact", valid_to=None):
    f = Fact(
        namespace="ns", subject_id=store._ent.id, predicate=predicate,
        fact_text=text, valid_at=valid_at, episode_id=store._ep.id,
        is_inference=is_inference, record_kind=record_kind, valid_to=valid_to,
    )
    full, coarse = store._emb.embed_with_coarse(text)
    store.add_fact(f, full, coarse)
    return f


def _row(store, fact_id):
    return store._db.execute(
        "SELECT is_latest, valid_to, superseded_by, invalidated_by "
        "FROM fact WHERE id=?",
        (fact_id,),
    ).fetchone()


# ══════════════════════════════════════════════════════════════════════════
# Step 1 — duplicate-cascade in supersede_fact (store-level unit)
# ══════════════════════════════════════════════════════════════════════════
def test_supersede_fact_returns_closed_ids_no_duplicates(tmp_path):
    """The new return value: with zero retired duplicates, supersede_fact returns
    exactly [old_id]."""
    s = _fresh_store(tmp_path)
    old = _add_fact(s, "user works at acme", valid_at=1_000)
    new = _add_fact(s, "user works at zorbex", valid_at=3_000)

    closed = s.supersede_fact(old.id, new.id, valid_to=3_000)
    assert closed == [old.id], "no duplicates → just the explicit target"
    assert _row(s, old.id)["valid_to"] == 3_000
    s.close()


def test_supersede_fact_cascade_closes_open_duplicate(tmp_path):
    """A retired duplicate (valid_to NULL, superseded_by=survivor) closes at the
    SAME world-time when its survivor is superseded, and is in the returned set."""
    s = _fresh_store(tmp_path)
    survivor = _add_fact(s, "user works at acme", valid_at=1_000)
    dup = _add_fact(s, "user works at acme", valid_at=2_000)
    s.retire_duplicate(dup.id, survivor.id)  # dup → survivor, valid_to NULL
    assert _row(s, dup.id)["valid_to"] is None  # precondition: open duplicate

    new = _add_fact(s, "user works at zorbex", valid_at=5_000)
    closed = s.supersede_fact(survivor.id, new.id, valid_to=5_000)

    assert set(closed) == {survivor.id, dup.id}, "cascade set includes the duplicate"
    assert _row(s, survivor.id)["valid_to"] == 5_000
    assert _row(s, dup.id)["valid_to"] == 5_000, "duplicate closed at the same V"
    # vec mirror already 0 from retire_duplicate; survivor's flips to 0 here.
    vec = s._db.execute(
        "SELECT is_latest FROM fact_vec WHERE fact_id=?", (survivor.id,)
    ).fetchone()
    assert vec["is_latest"] == 0
    s.close()


def test_supersede_fact_cascade_only_touches_open_duplicates(tmp_path):
    """The cascade UPDATE is scoped to superseded_by=old AND valid_to IS NULL, so
    an already-closed duplicate (from a prior supersession) is not re-closed."""
    s = _fresh_store(tmp_path)
    survivor = _add_fact(s, "user works at acme", valid_at=1_000)
    # A duplicate that was already world-time-closed (valid_to set) — NOT a
    # retire_duplicate row. It must be left alone by the cascade.
    already_closed = _add_fact(s, "user works at acme", valid_at=2_000, valid_to=2_500)
    s._db.execute(
        "UPDATE fact SET superseded_by=?, is_latest=0 WHERE id=?",
        (survivor.id, already_closed.id),
    )
    s._db.commit()

    new = _add_fact(s, "user works at zorbex", valid_at=5_000)
    closed = s.supersede_fact(survivor.id, new.id, valid_to=5_000)

    assert closed == [survivor.id], "already-closed duplicate not in the cascade set"
    assert _row(s, already_closed.id)["valid_to"] == 2_500, "its valid_to untouched"
    s.close()


# ══════════════════════════════════════════════════════════════════════════
# Step 3a — RESURRECTION (§10.2): dedup then supersede via ORDINARY ingest
# ══════════════════════════════════════════════════════════════════════════
def _latest_texts(mem, ns, predicate):
    store = mem._store(ns)
    rows = store._db.execute(
        "SELECT fact_text FROM fact WHERE predicate=? AND is_latest=1",
        (predicate,),
    ).fetchall()
    return [r["fact_text"] for r in rows]


def test_resurrection_dedup_then_ordinary_ingest_supersession(tmp_path):
    """Retire a duplicate B→A, then supersede A via a contradicting Memory.add.
    The cascade must close B at the same world-time as A, and a pure point-in-time
    as_of search after the supersession must return only the new fact — B must NOT
    resurrect on the is_latest_only=False surface."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    store = mem._store("ns")

    A = store._db.execute(
        "SELECT id, subject_id, valid_at FROM fact WHERE predicate='works_at' "
        "AND is_latest=1"
    ).fetchone()
    # Hand-inject an exact duplicate B in the same slot, then retire it onto A.
    B = Fact(
        namespace="ns", subject_id=A["subject_id"], predicate="works_at",
        fact_text="I work at Acme.", valid_at=2_000,
        episode_id=store._db.execute("SELECT id FROM episode LIMIT 1").fetchone()["id"],
    )
    full, coarse = mem.embedder.embed_with_coarse(B.fact_text)
    store.add_fact(B, full, coarse)
    store.retire_duplicate(B.id, A["id"])
    assert _row(store, B.id)["valid_to"] is None  # open retired duplicate

    # Ordinary ingest of a contradicting fact in the same functional slot.
    new_ids = mem.add("ns", "I work at Zorbex now.", t_ref=5_000)
    new_id = new_ids[0]
    new_valid_at = store.get_fact(new_id).valid_at

    # A closed by ordinary supersession; B closed by the duplicate-cascade at V.
    assert _row(store, A["id"])["valid_to"] == new_valid_at
    assert _row(store, B.id)["valid_to"] == new_valid_at, (
        "retired duplicate resurrected — cascade did not close it"
    )

    # Pure point-in-time as-of surface AFTER the supersession: only the new fact.
    hits = mem.search(
        "ns", "where do I work", k=10, as_of=new_valid_at + 1, is_latest_only=False
    )
    texts = {h.fact.fact_text for h in hits}
    assert "I work at Zorbex now." in texts
    assert "I work at Acme." not in texts, "Acme (A or B) visible after supersession"
    mem.close()


def test_transitive_resurrection_B_to_A_to_D_then_supersede(tmp_path):
    """The rev-3 killer: retire B→A, then A→D (chain re-points B→D), then supersede
    D via ordinary ingest. The single-level cascade must close BOTH A and B at V,
    because Task 1's chain invariant left both pointing directly at D."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    store = mem._store("ns")
    ep_id = store._db.execute("SELECT id FROM episode LIMIT 1").fetchone()["id"]
    D_row = store._db.execute(
        "SELECT id, subject_id FROM fact WHERE predicate='works_at' AND is_latest=1"
    ).fetchone()
    subj = D_row["subject_id"]

    def _inject(text, valid_at):
        f = Fact(namespace="ns", subject_id=subj, predicate="works_at",
                 fact_text=text, valid_at=valid_at, episode_id=ep_id)
        full, coarse = mem.embedder.embed_with_coarse(text)
        store.add_fact(f, full, coarse)
        return f

    A = _inject("I work at Acme.", 2_000)
    B = _inject("I work at Acme.", 3_000)
    store.retire_duplicate(B.id, A.id)      # B → A
    store.retire_duplicate(A.id, D_row["id"])  # A → D ; re-points B → D

    # Precondition (Task 1 behavior): B was re-pointed to D.
    assert _row(store, B.id)["superseded_by"] == D_row["id"], "B re-pointed to D"
    assert _row(store, A.id)["superseded_by"] == D_row["id"]

    new_ids = mem.add("ns", "I work at Zorbex now.", t_ref=6_000)
    new_valid_at = store.get_fact(new_ids[0]).valid_at

    assert _row(store, D_row["id"])["valid_to"] == new_valid_at
    assert _row(store, A.id)["valid_to"] == new_valid_at, "A not closed by cascade"
    assert _row(store, B.id)["valid_to"] == new_valid_at, "B not closed by cascade"

    # Standing invariant (spec §10.2): no open retired duplicate points at a
    # non-latest row.
    orphans = store._db.execute(
        """SELECT f.id FROM fact f JOIN fact t ON t.id = f.superseded_by
           WHERE f.superseded_by IS NOT NULL AND f.valid_to IS NULL
             AND t.is_latest = 0"""
    ).fetchall()
    assert orphans == []
    mem.close()


# ══════════════════════════════════════════════════════════════════════════
# Step 3c — STALE SUMMARY (§10.3): hand-inserted fixture
# ══════════════════════════════════════════════════════════════════════════
def test_stale_summary_flips_on_ordinary_ingest_supersession(tmp_path):
    """Hand-insert a summary fact derived from a source, then contradict the source
    via ordinary ingest. The staleness cascade must flip the summary is_latest=0,
    valid_to=new.valid_at, invalidated_by=new.id."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    store = mem._store("ns")
    ep_id = store._db.execute("SELECT id FROM episode LIMIT 1").fetchone()["id"]
    source = store._db.execute(
        "SELECT id, subject_id FROM fact WHERE predicate='works_at' AND is_latest=1"
    ).fetchone()

    # Hand-insert a summary row (record_kind='summary', predicate='summary',
    # is_inference=1) in its OWN slot, plus a derivation edge to the source.
    summary = Fact(
        namespace="ns", subject_id=source["subject_id"], predicate="summary",
        fact_text="The user has worked at Acme.", valid_at=1_500, episode_id=ep_id,
        is_inference=1, record_kind="summary",
    )
    full, coarse = mem.embedder.embed_with_coarse(summary.fact_text)
    store.add_fact(summary, full, coarse)
    store.add_derivation(summary.id, source["id"], run_id="run-x", created_at=1_500)

    # Vacuity guard FIRST: derivation must be non-empty, else the test is hollow.
    n_deriv = store._db.execute(
        "SELECT COUNT(*) AS c FROM fact_derivation WHERE source_id=?", (source["id"],)
    ).fetchone()["c"]
    assert n_deriv == 1, "fixture derivation row missing — test would pass vacuously"
    assert _row(store, summary.id)["is_latest"] == 1  # summary starts latest

    # Ordinary ingest contradicts the source (functional works_at slot).
    new_ids = mem.add("ns", "I work at Zorbex now.", t_ref=5_000)
    new_id = new_ids[0]
    new_valid_at = store.get_fact(new_id).valid_at

    srow = _row(store, summary.id)
    assert srow["is_latest"] == 0, "stale summary did not leave the default surface"
    assert srow["valid_to"] == new_valid_at
    assert srow["invalidated_by"] == new_id
    # vec mirror flipped too.
    vec = store._db.execute(
        "SELECT is_latest FROM fact_vec WHERE fact_id=?", (summary.id,)
    ).fetchone()
    assert vec["is_latest"] == 0
    mem.close()


def test_stale_summary_fires_for_cascade_closed_duplicate(tmp_path):
    """The load-bearing seam (spec §4.3): the staleness cascade keys off the FULL
    closed set returned by supersede_fact — including duplicate-cascade-closed
    rows — not just the loop's explicit targets. A summary derived from a retired
    DUPLICATE must still flip when the survivor is superseded."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    store = mem._store("ns")
    ep_id = store._db.execute("SELECT id FROM episode LIMIT 1").fetchone()["id"]
    survivor = store._db.execute(
        "SELECT id, subject_id FROM fact WHERE predicate='works_at' AND is_latest=1"
    ).fetchone()

    # A retired duplicate of the survivor (valid_to NULL).
    dup = Fact(namespace="ns", subject_id=survivor["subject_id"], predicate="works_at",
               fact_text="I work at Acme.", valid_at=2_000, episode_id=ep_id)
    full, coarse = mem.embedder.embed_with_coarse(dup.fact_text)
    store.add_fact(dup, full, coarse)
    store.retire_duplicate(dup.id, survivor["id"])

    # A summary derived ONLY from the retired duplicate (not the survivor).
    summary = Fact(namespace="ns", subject_id=survivor["subject_id"], predicate="summary",
                   fact_text="The user has worked at Acme.", valid_at=2_500,
                   episode_id=ep_id, is_inference=1, record_kind="summary")
    sfull, scoarse = mem.embedder.embed_with_coarse(summary.fact_text)
    store.add_fact(summary, sfull, scoarse)
    store.add_derivation(summary.id, dup.id, run_id="run-y", created_at=2_500)
    assert _row(store, summary.id)["is_latest"] == 1

    new_ids = mem.add("ns", "I work at Zorbex now.", t_ref=5_000)
    new_valid_at = store.get_fact(new_ids[0]).valid_at

    srow = _row(store, summary.id)
    assert srow["is_latest"] == 0, (
        "summary derived from a cascade-closed duplicate did not flip — the "
        "returned-set plumbing regressed"
    )
    assert srow["valid_to"] == new_valid_at
    assert srow["invalidated_by"] == new_ids[0]
    mem.close()


def test_staleness_cascade_leaves_unrelated_latest_summary_alone(tmp_path):
    """Guard: the cascade only invalidates summaries derived from the CLOSED
    sources. A summary derived from an UNAFFECTED source stays is_latest=1."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    mem.add("ns", "I live in Boston.", t_ref=1_000)
    store = mem._store("ns")
    ep_id = store._db.execute("SELECT id FROM episode LIMIT 1").fetchone()["id"]
    lives = store._db.execute(
        "SELECT id, subject_id FROM fact WHERE predicate='lives_in' AND is_latest=1"
    ).fetchone()

    # Summary derived from the lives_in source (untouched by a works_at contradiction).
    summary = Fact(namespace="ns", subject_id=lives["subject_id"], predicate="summary",
                   fact_text="The user lives in Boston.", valid_at=1_500,
                   episode_id=ep_id, is_inference=1, record_kind="summary")
    full, coarse = mem.embedder.embed_with_coarse(summary.fact_text)
    store.add_fact(summary, full, coarse)
    store.add_derivation(summary.id, lives["id"], run_id="run-z", created_at=1_500)

    mem.add("ns", "I work at Zorbex now.", t_ref=5_000)  # closes works_at, not lives_in

    assert _row(store, summary.id)["is_latest"] == 1, "unrelated summary wrongly flipped"
    mem.close()


# ══════════════════════════════════════════════════════════════════════════
# Step 4 — NO-OP PIN (§10.10): both cascades provably do nothing on a
# maintenance-naive DB
# ══════════════════════════════════════════════════════════════════════════
def test_hooks_are_noops_on_maintenance_naive_db(tmp_path):
    """A full multi-fact ingest with real supersessions on a fresh DB where
    maintenance NEVER ran: (i) no fact row has superseded_by set with valid_to
    NULL (so the duplicate-cascade had nothing to close), and (ii) fact_derivation
    is empty (so the staleness cascade had nothing to look up). Both cascades
    provably had no effect — the first-run path is unchanged."""
    mem = Memory(root=tmp_path)
    # A corpus with genuine ingest supersessions (functional slot replacements).
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    mem.add("ns", "I also work at Globex.", t_ref=2_000)  # co-valid extend
    mem.add("ns", "I work at Zorbex now.", t_ref=3_000)   # supersedes the slot
    mem.add("ns", "I live in Boston.", t_ref=4_000)
    mem.add("ns", "I live in Denver now.", t_ref=5_000)   # supersedes lives_in
    store = mem._store("ns")

    # (i) No open retired duplicate exists — the duplicate-cascade's trigger set is
    # empty (retire_duplicate is the ONLY writer of superseded_by-with-NULL-valid_to,
    # and maintenance never ran). Ordinary supersession always sets both together.
    open_supersedes = store._db.execute(
        "SELECT COUNT(*) AS c FROM fact WHERE superseded_by IS NOT NULL "
        "AND valid_to IS NULL"
    ).fetchone()["c"]
    assert open_supersedes == 0, "ordinary ingest left an open-superseded row"

    # (ii) fact_derivation is empty — the staleness cascade's lookup set is empty.
    n_deriv = store._db.execute("SELECT COUNT(*) AS c FROM fact_derivation").fetchone()["c"]
    assert n_deriv == 0, "no summarize ran, yet a derivation row exists"

    # And the ordinary supersessions still worked correctly (spine intact).
    assert _latest_texts(mem, "ns", "works_at") == ["I work at Zorbex now."]
    assert _latest_texts(mem, "ns", "lives_in") == ["I live in Denver now."]

    hits = mem.search("ns", "where do I work", k=5)
    assert any("Zorbex" in h.fact.fact_text for h in hits)
    mem.close()
