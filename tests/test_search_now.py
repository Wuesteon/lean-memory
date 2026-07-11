"""Recency anchoring: Memory.search(now=...) must reach the decay term.

Regression for phase2-learnings assumption #8 — with 2023 data read in 2026,
exp(-λ·age) ≈ 0 for every fact and the 0.2 recency weight de-ranks nothing.
"""
from lean_memory import Memory

MONTH_MS = 30 * 24 * 60 * 60 * 1000
T0 = 1_600_000_000_000  # a fixed historical epoch-ms


def _corpus(mem, ns):
    # Identical shape, different age. NOTE: the offline StubCandidateGenerator's
    # verb lexicon has no "adopt" entry (the brief's original wording produced
    # zero facts), so we use "have" — a lexicon verb — to exercise the same
    # two-fact, different-age setup the brief intends. Both land in the same
    # (user, has) slot; is_latest_only=False surfaces both regardless.
    mem.add(ns, "I have a cat.", t_ref=T0)
    mem.add(ns, "I have a dog.", t_ref=T0 + 11 * MONTH_MS)


def test_now_anchors_recency(tmp_path):
    mem = Memory(root=tmp_path)
    _corpus(mem, "anchored")
    hits = mem.search("anchored", "have", k=5, is_latest_only=False,
                      now=T0 + 12 * MONTH_MS)
    rec = {h.fact.fact_text: h.recency for h in hits}
    assert rec["I have a dog."] > 0.3     # 1 month old  → e^-1 ≈ 0.37
    assert rec["I have a cat."] < 0.001   # 12 months old → e^-12
    mem.close()


def test_default_now_is_wall_clock(tmp_path):
    mem = Memory(root=tmp_path)
    _corpus(mem, "wallclock")
    hits = mem.search("wallclock", "have", k=5, is_latest_only=False)
    assert hits  # sanity: the corpus produced facts
    assert all(h.recency < 0.001 for h in hits)  # historical corpus, real 'now'
    mem.close()
