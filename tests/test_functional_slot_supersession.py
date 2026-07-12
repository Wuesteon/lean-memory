"""A replacement on a FUNCTIONAL slot must retire every co-valid latest fact.

Regression for a review-board major (reproduced end-to-end through MCP with real
models): the resolver returns a single most-similar target, so when an additive
cue had left a functional slot holding N>1 co-valid facts ('I work at Acme.' +
'I also work at Globex.'), a later replacement ('I work at Zorbex now.') retired
only one of them — memory_search then returned two conflicting current
employers, violating the headline 'only the current fact is returned' claim.

Multi-valued slots (likes/uses/...) keep the single-target behavior: retiring
unrelated co-valid values there would be the opposite corruption.
"""

from lean_memory import Memory
from lean_memory.extract.contradiction import SUPERSEDES, Decision, is_multivalued


def _latest_in(mem: Memory, ns: str, predicate: str) -> list:
    store = mem._store(ns)
    rows = store._db.execute(
        "SELECT fact_text, superseded_by FROM fact WHERE predicate=? AND is_latest=1",
        (predicate,),
    ).fetchall()
    return [r["fact_text"] for r in rows]


def test_replacement_retires_all_covalid_facts_on_functional_slot(tmp_path):
    mem = Memory(root=tmp_path)
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    mem.add("ns", "I also work at Globex.", t_ref=2_000)  # additive cue → co-valid pair
    assert len(_latest_in(mem, "ns", "works_at")) == 2  # precondition: EXTENDS worked

    mem.add("ns", "I work at Zorbex now.", t_ref=3_000)

    latest = _latest_in(mem, "ns", "works_at")
    assert len(latest) == 1, f"stale co-valid facts survived the replacement: {latest}"
    assert "Zorbex" in latest[0]
    mem.close()


def test_multivalued_slot_supersession_retires_only_the_target(tmp_path):
    """The retire-all rule is scoped to functional predicates: on a multi-valued
    slot a SUPERSEDES decision (e.g. LLM-adjudicated in the ambiguous band) must
    retire only the matched value, not the user's other co-valid values."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I like jazz.", t_ref=1_000)
    mem.add("ns", "I also like blues.", t_ref=2_000)
    store = mem._store("ns")
    slot = store._db.execute(
        "SELECT id, subject_id FROM fact WHERE predicate='likes' AND is_latest=1"
    ).fetchall()
    assert len(slot) == 2

    ids = mem.add("ns", "I like bebop.", t_ref=3_000)  # lands in the same slot
    new_fact = store.get_fact(ids[0])
    slot_latest = store.find_latest_in_slot(new_fact.subject_id, "likes")
    target = next(f for f in slot_latest if "jazz" in f.fact_text)
    decision = Decision(label=SUPERSEDES, target=target, similarity=0.5, route="llm")

    mem._apply_supersession(store, decision, new_fact, slot_latest)

    latest = _latest_in(mem, "ns", "likes")
    assert any("blues" in t for t in latest), "unrelated co-valid value was retired"
    assert not any("jazz" in t for t in latest)
    mem.close()


def test_is_multivalued_helper():
    assert is_multivalued("likes")
    assert is_multivalued("uses")
    assert not is_multivalued("works_at")
    assert not is_multivalued("lives_in")
