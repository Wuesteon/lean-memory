"""Standing (query-free) value score for a fact — the EVICT signal (design spec §4.4).

This is NOT the retriever's query-time score; it is a query-independent "how much
is this fact worth keeping on the hot surface" number in [0, 1]. EVICT proposes /
auto-demotes facts that score low (§4.4).

    value = 0.5·(salience/10)
          + 0.3·exp(−DECAY_LAMBDA·(now − (last_access or valid_at)))
          + 0.2·min(1, log1p(access_count)/log1p(10))

The recency anchor is VERBATIM the retriever's — `(last_access or valid_at)`
(`retrieve/retriever.py:97`), reusing its `DECAY_LAMBDA` (imported, never
re-derived). This alignment is load-bearing: "stale" must mean the same thing to
EVICT as to the ranker. In particular a BACKFILLED fact — old `valid_at`, no
`last_access` yet — scores stale the moment it lands, exactly as the retriever
de-ranks it, even though it was just ingested. Using `created_at`/ingest time
instead (rev 1's bug) would score it fresh and diverge from the ranker (§4.4).
"""

from __future__ import annotations

import math

from ..retrieve.retriever import DECAY_LAMBDA  # reuse the ranker's decay — never re-derive
from ..types import Fact

# Weights (§4.4). Sum to 1.0, so `value` ∈ [0, 1].
_W_SALIENCE = 0.5
_W_RECENCY = 0.3
_W_ACCESS = 0.2

# Access-count saturation reference: log1p(10) — 10 hits saturates the term to ~1.
_ACCESS_SAT = math.log1p(10)


def value(fact: Fact, now: int) -> float:
    """Standing value of `fact` at wall-clock `now` (epoch ms), in [0, 1].

    Recency anchor is the retriever's `(last_access or valid_at)` verbatim; age is
    clamped at 0 (a future-dated fact is treated as maximally recent, matching the
    retriever's `max(0, ...)`).
    """
    salience_term = _W_SALIENCE * (fact.salience / 10.0)

    anchor = fact.last_access if fact.last_access else fact.valid_at
    age = max(0, now - anchor)
    recency_term = _W_RECENCY * math.exp(-DECAY_LAMBDA * age)

    access_term = _W_ACCESS * min(1.0, math.log1p(fact.access_count) / _ACCESS_SAT)

    return salience_term + recency_term + access_term
