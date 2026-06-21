"""Phase 0 spine tests. All run offline (FakeEmbedder + IdentityReranker), no downloads.

These prove the load-bearing Phase 0 behaviors:
  - ingest → extract → embed → store → retrieve round-trips
  - ADD-only supersession flips is_latest and chains superseded_by (never deletes)
  - as_of=T point-in-time query uses the world-time interval predicate
  - hybrid dense+BM25+RRF returns the relevant fact
  - Matryoshka truncation is deterministic and correctly normalized
"""

from __future__ import annotations

import numpy as np
import pytest

from lean_memory import Memory
from lean_memory.embed.base import matryoshka_truncate
from lean_memory.embed.fake import FakeEmbedder


@pytest.fixture
def mem(tmp_path):
    m = Memory(root=tmp_path)
    yield m
    m.close()


def test_ingest_and_search_roundtrip(mem):
    written = mem.add("u1", "I work at Acme.", t_ref=1_700_000_000_000)
    assert written, "extractor should emit at least one fact for a 'works at' sentence"
    hits = mem.search("u1", "where does the user work?", k=3)
    assert hits, "search should return the stored fact"
    assert any("Acme" in h.fact.fact_text for h in hits)


def test_per_namespace_isolation(mem, tmp_path):
    mem.add("alice", "I work at Acme.", t_ref=1_700_000_000_000)
    mem.add("bob", "I work at Globex.", t_ref=1_700_000_000_000)
    # separate files on disk (BET 4)
    assert (tmp_path / "alice.db").exists()
    assert (tmp_path / "bob.db").exists()
    alice_hits = mem.search("alice", "employer", k=5)
    assert all(h.fact.namespace == "alice" for h in alice_hits)
    assert not any("Globex" in h.fact.fact_text for h in alice_hits)


def test_add_only_supersession(mem):
    """A contradicting fact in the same (subject, predicate) slot supersedes the old
    one: old.is_latest flips to 0, old.superseded_by points to the new fact, and the
    old fact is NOT deleted (ADD-only)."""
    mem.add("u2", "I work at Acme.", t_ref=1_700_000_000_000)
    mem.add("u2", "I work at Globex.", t_ref=1_700_000_100_000)

    store = mem._store("u2")
    # find the works_at facts for the user
    rows = store._db.execute(
        "SELECT fact_text, is_latest, superseded_by FROM fact WHERE predicate='works_at' "
        "ORDER BY valid_at"
    ).fetchall()
    assert len(rows) == 2, "both facts retained (ADD-only, nothing deleted)"
    old, new = rows[0], rows[1]
    assert old["is_latest"] == 0 and old["superseded_by"] is not None
    assert new["is_latest"] == 1 and new["superseded_by"] is None

    # default search returns only the latest
    hits = mem.search("u2", "where does the user work?", k=5, is_latest_only=True)
    assert any("Globex" in h.fact.fact_text for h in hits)
    assert not any("Acme" in h.fact.fact_text for h in hits)


def test_as_of_point_in_time(mem):
    """as_of=T returns the fact that held at world-time T via the interval predicate."""
    t0 = 1_700_000_000_000
    t1 = 1_700_000_100_000
    mem.add("u3", "I work at Acme.", t_ref=t0)
    mem.add("u3", "I work at Globex.", t_ref=t1)

    # As of a time before the change, the Acme fact was valid.
    before = mem.search("u3", "employer", k=5, as_of=t0 + 1, is_latest_only=False)
    texts_before = " ".join(h.fact.fact_text for h in before)
    assert "Acme" in texts_before


def test_matryoshka_deterministic_and_normalized():
    emb = FakeEmbedder(dim=768, coarse_dim=256)
    v1 = emb.embed_one("hello world")
    v2 = emb.embed_one("hello world")
    assert np.allclose(v1, v2), "same text → same vector (reproducible)"
    coarse = matryoshka_truncate(v1, 256)
    assert coarse.shape == (256,)
    assert abs(float(np.linalg.norm(coarse)) - 1.0) < 1e-5, "coarse vec is L2-normalized"


def test_fake_embedder_distinguishes_text():
    emb = FakeEmbedder()
    a = emb.embed_one("the user works at Acme")
    b = emb.embed_one("the user lives in Berlin")
    assert not np.allclose(a, b)
