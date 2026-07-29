"""An identical restatement must not create a duplicate latest fact.

WP11 regression (surfaced by the lean-memory-sim longrun-18 study): the
resolver correctly refuses to let a verbatim restatement supersede anything
('identical restatement =/=> a change'), but add() then persisted the fact
unconditionally — every restatement inserted a new latest row in the same
slot, so store size and retrieval noise grew linearly with conversational
repetition. Write-time dedupe is EXACT-text against LATEST facts in the slot
only; near-duplicates stay with WP10a's offline dedup_near band.
"""

from lean_memory import Memory


def _facts_in(mem: Memory, ns: str, predicate: str) -> list:
    store = mem._store(ns)
    return store._db.execute(
        "SELECT fact_text, is_latest FROM fact WHERE predicate=?", (predicate,)
    ).fetchall()


def test_identical_restatement_is_not_persisted_twice(tmp_path):
    mem = Memory(root=tmp_path)
    first = mem.add("ns", "I work at Acme.", t_ref=1_000)
    again = mem.add("ns", "I work at Acme.", t_ref=2_000)

    rows = _facts_in(mem, "ns", "works_at")
    assert len(rows) == 1, f"restatement created duplicate rows: {[r['fact_text'] for r in rows]}"
    assert first, "first assertion must be written"
    assert not again, "restatement write must be skipped, returning no fact ids"
    mem.close()


def test_trivial_formatting_variants_are_treated_as_restatements(tmp_path):
    """Case, whitespace, and edge-punctuation variants must hit the skip too:
    on the default stub embedder the resolver's similarity bands are not
    trustworthy, so without deterministic normalization a case variant can
    land ambiguous and stack a SECOND co-valid latest row (observed)."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    mem.add("ns", "I work at Acme,", t_ref=2_000)
    mem.add("ns", "I WORK AT Acme.", t_ref=3_000)
    mem.add("ns", "I  work at\tAcme.", t_ref=4_000)

    rows = _facts_in(mem, "ns", "works_at")
    assert len(rows) == 1, f"formatting variants stacked rows: {[r['fact_text'] for r in rows]}"
    mem.close()


def test_entity_case_variant_splits_the_slot_known_limit(tmp_path):
    """KNOWN LIMIT, pinned: a case variant of the ENTITY surface form ('acme'
    vs 'Acme') resolves to a different entity via upsert_entity's BINARY-
    collation name match, so it lands in a different slot and bypasses both
    restatement dedupe and contradiction resolution. Fixing this is an
    entity-resolution change (collation policy on every lookup, with real
    distinct-by-case counterexamples like 'Polish'/'polish') — out of WP11
    scope. If this test starts failing with 1 row, that fix landed: fold the
    lowercase variant into the trivial-variants test above and delete this."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    mem.add("ns", "i work at acme.", t_ref=2_000)

    rows = _facts_in(mem, "ns", "works_at")
    assert len(rows) == 2
    mem.close()


def test_internal_punctuation_difference_is_not_a_restatement(tmp_path):
    """Internal punctuation can carry meaning — normalization strips edges
    only, so these must fall through to contradiction resolution, not skip."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "My budget is 10,5 euros.", t_ref=1_000)
    mem.add("ns", "My budget is 105 euros.", t_ref=2_000)

    store = mem._store("ns")
    texts = [
        r["fact_text"]
        for r in store._db.execute("SELECT fact_text FROM fact").fetchall()
    ]
    assert any("10,5" in t for t in texts)
    assert any("105 euros" in t for t in texts)
    mem.close()


def test_reasserting_a_superseded_value_still_supersedes(tmp_path):
    """Dedupe compares against LATEST facts only: re-asserting an old,
    superseded value is a genuine change and must retire the current one."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    mem.add("ns", "I work at Zorbex now.", t_ref=2_000)
    mem.add("ns", "I work at Acme.", t_ref=3_000)  # back again — differs from latest

    latest = [r for r in _facts_in(mem, "ns", "works_at") if r["is_latest"]]
    assert len(latest) == 1, f"expected one current employer: {[r['fact_text'] for r in latest]}"
    assert "Acme" in latest[0]["fact_text"]
    mem.close()
