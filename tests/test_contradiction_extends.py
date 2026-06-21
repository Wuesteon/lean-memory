"""Regression tests for the extends/supersedes fix (BET-2 audit finding).

The audit found the resolver mapped EVERY non-identical object to `supersedes` — it
could not represent a multi-valued slot (co-valid `extends`). These tests pin the two
behaviors the engine must distinguish, using FakeEmbedder (offline, deterministic):

  - functional slot + different object        → supersedes (replacement)
  - multi-valued predicate / additive cue     → extends   (co-valid, both stay latest)
"""

from __future__ import annotations

import pytest

from lean_memory import Memory
from lean_memory.embed.fake import FakeEmbedder
from lean_memory.extract.contradiction import (
    EXTENDS,
    SUPERSEDES,
    ContradictionResolver,
)
from lean_memory.types import Fact, new_id


def _fact(predicate, fact_text, obj, *, subject_id="user", valid_at=0):
    return Fact(
        namespace="t", subject_id=subject_id, predicate=predicate,
        fact_text=fact_text, object_literal=obj, valid_at=valid_at,
        episode_id="e", id=new_id(),
    )


@pytest.fixture
def resolver():
    return ContradictionResolver()


def test_functional_slot_different_object_supersedes(resolver):
    """works_at is functional: a new employer REPLACES the old one."""
    emb = FakeEmbedder()
    old = _fact("works_at", "I work at Acme.", "Acme")
    new = _fact("works_at", "I work at Globex.", "Globex", valid_at=1)
    d = resolver.classify(new, [old], emb)
    assert d.label == SUPERSEDES, f"expected supersedes, got {d.label} via {d.route}"
    assert d.target is not None


def test_multivalued_predicate_different_object_extends(resolver):
    """uses is multi-valued: a new tool ADDS to the slot (co-valid), not replaces."""
    emb = FakeEmbedder()
    old = _fact("uses", "I use Python.", "Python")
    new = _fact("uses", "I use Rust.", "Rust", valid_at=1)
    d = resolver.classify(new, [old], emb)
    assert d.label == EXTENDS, f"expected extends, got {d.label} via {d.route}"


def test_additive_cue_forces_extends(resolver):
    """An explicit 'also' makes even a functional-looking slot additive."""
    emb = FakeEmbedder()
    old = _fact("has", "I have a dog.", "dog")
    new = _fact("has", "I also have a cat.", "cat", valid_at=1)
    d = resolver.classify(new, [old], emb)
    assert d.label == EXTENDS, f"expected extends, got {d.label} via {d.route}"


def test_extends_keeps_both_facts_latest_end_to_end(tmp_path):
    """Through the full Memory pipeline: an extends fact leaves BOTH rows is_latest=1."""
    m = Memory(root=tmp_path, embedder=FakeEmbedder())
    m.add("u", "I use Python.", t_ref=1_700_000_000_000)
    m.add("u", "I also use Rust.", t_ref=1_700_000_100_000)
    store = m._store("u")
    rows = store._db.execute(
        "SELECT fact_text, is_latest FROM fact WHERE predicate='uses' ORDER BY valid_at"
    ).fetchall()
    m.close()
    assert len(rows) == 2
    # both co-valid → both latest (the bug would have flipped Python to is_latest=0)
    assert all(r["is_latest"] == 1 for r in rows), \
        f"extends must keep both latest, got {[(r['fact_text'], r['is_latest']) for r in rows]}"


def test_supersede_still_retires_old_end_to_end(tmp_path):
    """The replacement path is NOT broken by the fix: a functional slot still supersedes."""
    m = Memory(root=tmp_path, embedder=FakeEmbedder())
    m.add("u", "I work at Acme.", t_ref=1_700_000_000_000)
    m.add("u", "I work at Globex.", t_ref=1_700_000_100_000)
    store = m._store("u")
    rows = store._db.execute(
        "SELECT fact_text, is_latest FROM fact WHERE predicate='works_at' ORDER BY valid_at"
    ).fetchall()
    m.close()
    assert len(rows) == 2
    assert rows[0]["is_latest"] == 0 and rows[1]["is_latest"] == 1
