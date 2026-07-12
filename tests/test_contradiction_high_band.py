"""High-similarity band must respect the additive signal (multi-valued slots).

Regression for a review-board major: classify() consulted _is_additive() in the
LOW and AMBIGUOUS cosine bands but not the HIGH band. With the real default
embedder, distinct co-valid values on the same slot (jazz/blues, Python/Rust)
embed at cosine 0.6-0.95, so two genuinely co-valid values on a multi-valued
predicate landed in the high band, failed the token-subsumption refinement test,
and were classified SUPERSEDES — silently retiring a fact the user still holds.
Invisible offline because FakeEmbedder rarely places distinct texts that high.
"""

import math

import numpy as np

from lean_memory.extract.contradiction import (
    EXTENDS,
    SUPERSEDES,
    ContradictionResolver,
)
from lean_memory.types import Fact, new_id


class PinnedEmbedder:
    """Duck-typed embedder placing chosen text pairs at an exact cosine."""

    def __init__(self, cosine: float):
        self._vecs = {}
        self._cos = cosine

    def embed_one(self, text: str) -> np.ndarray:
        if text not in self._vecs:
            if not self._vecs:
                v = np.array([1.0, 0.0], dtype=np.float32)
            else:
                v = np.array(
                    [self._cos, math.sqrt(1 - self._cos**2)], dtype=np.float32
                )
            self._vecs[text] = v
        return self._vecs[text]


def _fact(predicate: str, obj: str, text: str) -> Fact:
    return Fact(
        id=new_id(), namespace="ns", subject_id="subj", predicate=predicate,
        object_literal=obj, fact_text=text, valid_at=0,
        episode_id="ep", ingested_at=0, created_at=0,
    )


def test_high_band_multivalued_predicate_extends():
    """'likes jazz' + 'likes blues' at cosine 0.85 must stay co-valid."""
    resolver = ContradictionResolver()
    embedder = PinnedEmbedder(cosine=0.85)
    existing = _fact("likes", "jazz", "I like jazz.")
    embedder.embed_one("jazz")  # pin jazz first so blues gets the 0.85 vector
    new = _fact("likes", "blues", "I like blues.")

    d = resolver.classify(new, [existing], embedder)
    assert d.label == EXTENDS
    assert d.similarity is not None and d.similarity >= resolver.high_sim


def test_high_band_functional_predicate_still_supersedes():
    """A restated change on a functional slot (works_at) must still replace."""
    resolver = ContradictionResolver()
    embedder = PinnedEmbedder(cosine=0.85)
    existing = _fact("works_at", "Acme", "I work at Acme.")
    embedder.embed_one("Acme")
    new = _fact("works_at", "Globex", "I work at Globex.")

    d = resolver.classify(new, [existing], embedder)
    assert d.label == SUPERSEDES
    assert d.route == "high_supersedes"


def test_high_band_functional_slot_ignores_conjunction_cue():
    """The high-band co-validity signal is the PREDICATE, not a textual cue: a
    replacement phrased with a conjunction ('I left Acme and now work at
    Globex.') must still supersede on a functional slot — the stray 'and'
    matches _ADDITIVE_CUE but says nothing about co-validity at high cosine."""
    resolver = ContradictionResolver()
    embedder = PinnedEmbedder(cosine=0.85)
    existing = _fact("works_at", "Acme", "I work at Acme.")
    embedder.embed_one("Acme")
    new = _fact("works_at", "Globex", "I left Acme and now work at Globex.")

    d = resolver.classify(new, [existing], embedder)
    assert d.label == SUPERSEDES
    assert d.route == "high_supersedes"
