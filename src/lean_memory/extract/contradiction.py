"""Cheap-then-escalate contradiction → supersession resolver (spec section 5).

When a new fact lands on an existing `(subject_id, predicate)` slot we must decide,
*without paying for an LLM call on the common path*, whether the new fact:

  - ``asserts``    — first fact in the slot (nothing to compare against), or
  - ``extends``    — refines/adds detail to the slot WITHOUT contradicting it
                     (both rows stay co-valid, `is_latest=1` on both), or
  - ``supersedes`` — contradicts/replaces the slot's object
                     (old row gets `valid_to`/`superseded_by`/`is_latest=0`).

The pipeline is the spec's Engram-style "cheap-then-escalate" ladder, which exists
specifically to AVOID a per-fact LLM call (BET 2's cost story dies if every write
escalates):

  1. Slot match — done by the CALLER, which passes `existing_latest_facts` already
     filtered to the same (subject_id, predicate) slot (via
     `store.find_latest_in_slot`). If empty → ``asserts``, no embedder/LLM touched.
  2. Object-embedding cosine — compare the new object's text to each existing
     object's text using the SAME embedder the store uses (L2-normalized vectors,
     so cosine == dot product). Deterministic given a fixed embedder.
  3. Subsumption heuristic — high cosine + a clean refinement signal (one object
     text subsumes the other as a token-superset/substring) ⇒ ``extends`` (co-valid);
     low cosine on the same slot ⇒ a clear contradiction ⇒ ``supersedes``.
  4. LLM adjudication ONLY in the ambiguous middle band (cosine neither clearly
     "same/refinement" nor clearly "different") AND only when an `llm_typer` is
     supplied. With no typer (offline default) we fall back to the SAFER choice:
     `supersedes`, because silently keeping two co-valid contradictory facts in one
     slot corrupts `WHERE is_latest=1` current-state reads, whereas an
     over-eager supersede is recoverable (ADD-only: nothing is deleted).

Everything here is pure logic + the embedder; the LLM is optional and injected
behind the `LLMTyper` protocol, so the whole resolver is deterministic and
offline-testable exactly like FakeEmbedder / IdentityReranker in Phase 0. A
deterministic stub that satisfies `LLMTyper` keeps the ambiguous-band path
testable with zero servers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, runtime_checkable

import numpy as np

from ..embed.base import Embedder
from ..types import Fact

# ── relation taxonomy (spec section 5) ────────────────────────────────────────
# Only the three *structural* relations are decidable here. `derives` is the
# inferential, LLM-only edge (is_inference=1) and is emitted by the typing pass,
# never by contradiction resolution — so it is intentionally absent from this set.
ASSERTS = "asserts"
EXTENDS = "extends"
SUPERSEDES = "supersedes"

#: Public type for the structural label this resolver can emit.
RelationLabel = str  # one of {ASSERTS, EXTENDS, SUPERSEDES}

# ── routing thresholds (the cheap-then-escalate bands) ────────────────────────
# Cosine over object texts partitions into three regions:
#   sim >= HIGH   → near-identical / refinement candidate → cheap subsumption check
#   sim <= LOW    → clearly different object on the same slot → contradiction
#   LOW < sim < HIGH → AMBIGUOUS → escalate to the LLM (if any), else default safe
# Calibrated against the default real embedder (Qwen3-Embedding-0.6B / EmbeddingGemma):
# distinct short noun-phrase objects on the SAME slot empirically embed at cosine
# ~0.6–0.95 (they share slot/topic context), so the naive "different object ⇒ low
# cosine" assumption is FALSE for these models. HIGH marks "essentially the same value
# (rephrase/refine)"; LOW marks "clearly unrelated value". The wide middle is where the
# additive-vs-replacement question actually lives — decided by the additive signal below
# (cheap) then the LLM (only if still ambiguous). Per-embedder tunable via __init__.
DEFAULT_HIGH_SIM = 0.80
DEFAULT_LOW_SIM = 0.45

_WORD = re.compile(r"[a-z0-9]+")

# ── additive-vs-replacement signal (the fix for extends being unreachable) ────────
# A DIFFERENT object on the same slot is NOT automatically a contradiction: it can be
# ADDITIVE (co-valid → extends) rather than REPLACING (→ supersedes). Two cheap signals
# say "additive", deterministically, no LLM:
#   (a) an explicit additive cue in the new fact text ("also", "and", "too", ...), or
#   (b) an inherently MULTI-VALUED predicate (you can like/use/know many things at once),
#       as opposed to a FUNCTIONAL predicate (one current employer / city / birthplace).
# Without either signal, a different object on a functional slot is a replacement.
_ADDITIVE_CUE = re.compile(
    r"\b(?:also|too|as\s+well|in\s+addition|additionally|and|plus|another|"
    r"besides|moreover|on\s+the\s+side)\b",
    re.I,
)

#: Predicates that naturally hold MANY co-valid values at once → a new distinct object
#: extends the slot rather than replacing it. Everything not listed is treated as
#: FUNCTIONAL (single current value) so a distinct object supersedes.
_MULTIVALUED_PREDICATES = frozenset(
    {"likes", "dislikes", "uses", "knows", "has", "owns", "speaks", "plays",
     "member_of", "interested_in", "skilled_in"}
)


@runtime_checkable
class LLMTyper(Protocol):
    """Pass-4 constrained-typing backend, narrowed to the adjudication this resolver needs.

    The full Pass-4 typer (Ollama-backed, with a deterministic stub) does more —
    relation typing, coreference, cross-utterance edges — but contradiction
    resolution only needs a single constrained call: *given the new fact and one
    contradicting-candidate existing fact, is this `extends` or `supersedes`?*
    Keeping the surface this small means a trivial stub satisfies it for tests, and
    any richer typer that exposes `adjudicate_contradiction` plugs in unchanged.

    Implementations MUST return one of {EXTENDS, SUPERSEDES} (never ASSERTS — there
    is, by construction, an existing fact in the slot when we escalate) and MUST be
    callable offline via a deterministic stub (mirrors the Ollama→ConnectionError
    fallback in the typing module: a real typer falls back to its stub, so this
    method never raises a transport error up into the resolver).
    """

    def adjudicate_contradiction(self, new_fact: Fact, existing_fact: Fact) -> RelationLabel: ...


@dataclass
class Decision:
    """Outcome of resolution — carries everything `memory.py` needs to act ADD-only.

    `label`:
        ASSERTS    → just write the new fact; touch nothing else.
        EXTENDS    → write the new fact; the matched fact stays co-valid (no supersede).
        SUPERSEDES → write the new fact, then `store.supersede_fact(target.id, new.id,
                     valid_to=new.valid_at)` on the target.
    `target`:
        The existing fact the new one supersedes (only set for SUPERSEDES; the matched
        fact for EXTENDS is exposed too for logging/links). None for ASSERTS.
    `similarity`:
        Best object cosine in [-1, 1] against the slot (None when no existing fact).
    `route`:
        Which rung of the ladder decided it — 'no_slot' | 'high_extends' |
        'high_supersedes' | 'low_supersedes' | 'llm' | 'ambiguous_default'. This is
        the observability hook the BET-2 escalation-rate metric reads (route=='llm'
        is the only rung that spent an LLM call).
    """

    label: RelationLabel
    target: Optional[Fact] = None
    similarity: Optional[float] = None
    route: str = "no_slot"

    @property
    def escalated(self) -> bool:
        """True iff this decision spent an LLM call — summed by the router's metric."""
        return self.route == "llm"


class ContradictionResolver:
    """Decide asserts/extends/supersedes for a new fact against its slot.

    Pure + embedder-driven; `llm_typer` optional. Construct once and reuse across
    writes — it is stateless apart from its thresholds.
    """

    def __init__(
        self,
        *,
        high_sim: float = DEFAULT_HIGH_SIM,
        low_sim: float = DEFAULT_LOW_SIM,
    ) -> None:
        if not (0.0 <= low_sim <= high_sim <= 1.0):
            raise ValueError("require 0 <= low_sim <= high_sim <= 1")
        self.high_sim = high_sim
        self.low_sim = low_sim

    def classify(
        self,
        new_fact: Fact,
        existing_latest_facts: Sequence[Fact],
        embedder: Embedder,
        *,
        llm_typer: Optional[LLMTyper] = None,
    ) -> Decision:
        """Classify `new_fact` against the (already slot-filtered) `existing_latest_facts`.

        The caller is responsible for step 1 (slot match): `existing_latest_facts`
        MUST already be the latest facts sharing `new_fact`'s (subject_id, predicate)
        slot. We defensively re-filter by slot and drop exact-text duplicates so a
        loose caller can't make us supersede a fact by an identical restatement.
        """
        # Step 1 — slot match. Defensive re-filter (caller should have done this).
        candidates = [
            f
            for f in existing_latest_facts
            if f.subject_id == new_fact.subject_id
            and f.predicate == new_fact.predicate
            and f.id != new_fact.id
            and f.fact_text != new_fact.fact_text  # identical restatement ⇒ not a change
        ]
        if not candidates:
            # Empty slot (or only an identical restatement) → nothing to contradict.
            return Decision(label=ASSERTS, target=None, similarity=None, route="no_slot")

        # Step 2 — object-embedding cosine. Compare against every slot candidate and
        # resolve against the MOST-similar one (the strongest contradiction/refinement
        # signal). Embeddings are L2-normalized → cosine is a plain dot product.
        new_vec = embedder.embed_one(_object_text(new_fact))
        best: Optional[Fact] = None
        best_sim = -1.0
        for cand in candidates:
            sim = _cosine(new_vec, embedder.embed_one(_object_text(cand)))
            if sim > best_sim:
                best_sim, best = sim, cand
        assert best is not None  # candidates is non-empty

        # Step 3 — cheap subsumption / contradiction by band.
        if best_sim >= self.high_sim:
            # Near-identical object text. Decide refinement vs replacement by a
            # token-subsumption heuristic — no LLM needed for the clear cases.
            if _is_refinement(new_fact, best):
                return Decision(
                    label=EXTENDS, target=best, similarity=best_sim, route="high_extends"
                )
            # Same object, no added detail and not equal text ⇒ a restated change
            # (e.g. "moved to Berlin" vs "moved to Munich" that still embed close):
            # treat as a replacement on the slot.
            return Decision(
                label=SUPERSEDES, target=best, similarity=best_sim, route="high_supersedes"
            )

        if best_sim <= self.low_sim:
            # A clearly DIFFERENT object on the same slot. This is NOT automatically a
            # contradiction: a distinct value can be additive (co-valid → extends) or a
            # replacement (→ supersedes). Decide cheaply by the additive signal.
            if _is_additive(new_fact):
                return Decision(
                    label=EXTENDS, target=best, similarity=best_sim, route="low_extends"
                )
            return Decision(
                label=SUPERSEDES, target=best, similarity=best_sim, route="low_supersedes"
            )

        # Step 4 — ambiguous middle band. A cheap additive cue still resolves it
        # without an LLM (an explicit "also" is unambiguous); otherwise escalate to the
        # LLM ONLY here, and only if one was supplied — the single rung that costs a call.
        if _is_additive(new_fact):
            return Decision(
                label=EXTENDS, target=best, similarity=best_sim, route="mid_extends"
            )
        if llm_typer is not None:
            label = llm_typer.adjudicate_contradiction(new_fact, best)
            label = label if label in (EXTENDS, SUPERSEDES) else SUPERSEDES
            return Decision(label=label, target=best, similarity=best_sim, route="llm")

        # No typer (offline default): pick the SAFER option. Supersede keeps the slot
        # single-valued so `WHERE is_latest=1` stays correct; nothing is deleted, so
        # an over-eager supersede is fully recoverable from the audit chain.
        return Decision(
            label=SUPERSEDES, target=best, similarity=best_sim, route="ambiguous_default"
        )


# ── helpers ───────────────────────────────────────────────────────────────────
def _object_text(fact: Fact) -> str:
    """Text we embed/compare for contradiction.

    Prefer the typed `object_literal` (the parsed slot value — the thing that
    actually contradicts), and fall back to the standalone `fact_text` when the
    object is an entity ref or wasn't separated out. Rules-extracted facts in
    Phase 0 carry no `object_literal`, so the `fact_text` fallback is the common path.
    """
    if fact.object_literal:
        return fact.object_literal
    return fact.fact_text


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. Embedder vectors are L2-normalized, but renormalize
    defensively so a non-normalizing embedder can't produce out-of-range values."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _is_refinement(new_fact: Fact, existing: Fact) -> bool:
    """Cheap refinement test: does one object's token set subsume the other's?

    `extends` means the new fact ADDS detail to the same slot without contradiction.
    The cheapest reliable signal at high cosine is token subsumption: if the new
    object's content words are a strict superset of the existing one's (or vice
    versa), it's adding/dropping qualifiers on the SAME value, not asserting a
    different value — so the two are co-valid. Disjoint-but-similar token sets at
    high cosine are NOT a refinement (handled as a restated change upstream).
    """
    new_toks = _tokens(_object_text(new_fact))
    old_toks = _tokens(_object_text(existing))
    if not new_toks or not old_toks:
        return False
    # Strict superset/subset in either direction → pure add/drop of qualifiers.
    return new_toks > old_toks or old_toks > new_toks


def _is_additive(new_fact: Fact) -> bool:
    """Does this new fact ADD a co-valid value to the slot (→ extends) rather than
    replace the existing one (→ supersedes)?

    A distinct object on the same slot is additive when EITHER an explicit additive
    cue appears in the fact text ("I *also* use Rust") OR the predicate is inherently
    multi-valued (you can `uses` many tools at once). Functional predicates (works_at,
    lives_in) hold one current value, so a distinct object there is a replacement.
    Deterministic — no LLM. This is what makes multi-valued slots representable.
    """
    if _ADDITIVE_CUE.search(new_fact.fact_text or ""):
        return True
    return new_fact.predicate in _MULTIVALUED_PREDICATES


def escalation_rate(decisions: Sequence[Decision]) -> float:
    """Fraction of decisions that spent an LLM call — the BET-2 first-class metric.

    Spec target: < 0.20. `memory.py` (or the ablation harness) accumulates the
    Decisions from a batch of writes and reports this; route=='llm' is the only
    rung that escalated. Returns 0.0 for an empty batch.
    """
    if not decisions:
        return 0.0
    return sum(1 for d in decisions if d.escalated) / len(decisions)
