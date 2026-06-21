"""Pass 3 — the recall-biased router (design-spec §5, Pass 3).

After Pass 1 (rules) and Pass 2 (GLiNER2) over-generate high-recall candidates, the
vast majority are *trivially explicit, high-confidence, intra-utterance* facts that a
local LLM would only rubber-stamp at a cost. The router's job is to spend the LLM
budget only where deterministic extraction is known to fail (BET 2, corrected 2026-06:
GLiNER2-class relation typing is weak — ~17.8% zero-shot Micro-F1 — and inferential /
cross-turn edges are exactly the residual the LLM must own).

A candidate is ESCALATED to the Pass-4 LLM-typing batch if ANY of:
  1. GLiNER2 confidence below `conf_threshold`          — the parser is unsure;
  2. coreference / ellipsis / zero-pronoun detected      — the span isn't self-contained;
  3. it references a *previously-seen* entity that was    — cross-turn / cross-session edges
     NOT introduced in this episode (`known_entities`)      are where deterministic isolation fails;
  4. it is a possible `derives` (inferential) edge        — only the LLM may emit `is_inference=1`.
Everything else is routed `direct` (skips the LLM) and gets the cheap `asserts`/slot path.

WHY this is its own deterministic pass (no model): the router IS the cost story. The
spec gates the whole BET-2 design on escalation rate staying < 20%; if it trends to
100% the hybrid is no cheaper than 100%-LLM and we revisit. So the router must be pure
stdlib, fully reproducible, and — critically — must surface its escalation rate as a
first-class, inspectable metric (`last_stats`) that the BET-2 ablation harness reads.

Like FakeEmbedder / IdentityReranker in Phase 0, this is the always-offline default:
zero downloads, zero servers, deterministic. The real GLiNER2 (Pass 2) and Ollama LLM
(Pass 4) sit behind their own interfaces with their own stubs; the router never calls
either — it only *decides* who gets escalated, from the candidate metadata alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# `Candidate` is the Pass-2 output contract, owned by the sibling taxonomy module.
# We import the symbol for type-hinting (the load-bearing module boundary), but read
# its fields through defensive accessors below so a parallel-built `Candidate` whose
# exact attribute names differ slightly still routes correctly rather than crashing.
from .taxonomy import Candidate

# ── escalation reason codes (stable strings → cheap to assert on / aggregate) ──
REASON_LOW_CONF = "low_confidence"
REASON_COREF = "coreference"
REASON_PRIOR_ENTITY = "prior_entity"
REASON_DERIVES = "derives"
REASON_PRE_FLAGGED = "pre_flagged"  # Pass-2 `needs_typing` already requested typing

# The relation taxonomy. `derives` is LLM-only (is_inference=1); the deterministic
# passes may only ever propose the structural three. An unknown/empty predicate is
# itself a signal that typing is needed, so it escalates.
_STRUCTURAL_RELATIONS = frozenset({"asserts", "supersedes", "extends"})
_KNOWN_PREDICATES = frozenset(
    {
        # The full slot lexicon emitted by Pass 1 (rules) AND Pass 2 (gliner_extractor
        # DEFAULT_RELATION_TYPES + _VERB_RELATIONS). Must stay a SUPERSET of both, else
        # a confidently-typed predicate gets mis-escalated as "possible derives" and the
        # escalation rate blows past the BET-2 <20% target on the offline default.
        "works_at",
        "lives_in",
        "located_in",
        "likes",
        "dislikes",
        "is_a",
        "has",
        "uses",
        "knows",
        # gliner_extractor DEFAULT_RELATION_TYPES extras not in the original list:
        "owns",
        "member_of",
        # common predicates the typer and gold set exercise (commutes_by, speaks, etc.
        # are *inferential* slots — they stay out; the ones below are explicit):
        "drives",
        "speaks",
        "plays",
        "skilled_in",
        "interested_in",
        "works_in",
        "commutes_by",
    }
) | _STRUCTURAL_RELATIONS

# ── coreference / ellipsis heuristics ──
# Pronouns and demonstratives that make a span non-self-contained. Word-boundaried and
# case-insensitive. "zero-pronoun" (elided subject) is approximated structurally below.
_COREF_PRONOUNS = re.compile(
    r"\b("
    r"he|him|his|she|her|hers|they|them|their|theirs|"
    r"it|its|this|that|these|those|"
    r"the\s+former|the\s+latter|the\s+same|"
    r"there|then"
    r")\b",
    re.I,
)

# Inferential cue words → a candidate that *might* be a `derives` edge. The router only
# flags it for the LLM to confirm; the router itself NEVER assigns `derives`.
_INFERENCE_CUES = re.compile(
    r"\b("
    r"so|therefore|thus|hence|because|since|"
    r"must|probably|likely|presumably|implies|imply|implied|"
    r"means|suggests?|consequently|as\s+a\s+result"
    r")\b",
    re.I,
)

# A leading conjunction / verb with no overt subject ⇒ likely an elided (zero-pronoun)
# subject carried from the prior clause/turn ("...and moved to Berlin", "Then joined X").
_ELLIPSIS_LEAD = re.compile(r"^\s*(and|but|then|also|plus|so)\b", re.I)
_LEADING_VERB = re.compile(
    r"^\s*(works?|lives?|moved|joined|left|likes?|loves?|hates?|uses?|has|had|is|are|was|were)\b",
    re.I,
)


@dataclass
class RouteStats:
    """Escalation-rate metrics — the router's first-class, inspectable output.

    `rate` is escalated/seen (0.0 when nothing was seen). The BET-2 ablation harness
    reads this to assert the < 20% target and to log tokens-saved vs 100%-LLM.
    `by_reason` breaks the escalations down so we can see *why* the budget is spent.
    """

    seen: int = 0
    escalated: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return (self.escalated / self.seen) if self.seen else 0.0

    def as_dict(self) -> dict:
        # Matches the spec's required {seen, escalated, rate} shape, plus the breakdown.
        return {
            "seen": self.seen,
            "escalated": self.escalated,
            "rate": self.rate,
            "by_reason": dict(self.by_reason),
        }


# ── candidate-field accessors (defensive: tolerate sibling-module naming drift) ──
def _cand_confidence(c: Candidate) -> float:
    """GLiNER2 (or rules) confidence in [0,1]. Missing ⇒ 0.0 ⇒ escalate (recall-biased).

    Reads `confidence` (taxonomy/gliner contract) and falls back to `gliner_confidence`
    so a Candidate carrying only the typer-side confidence name still routes correctly.
    """
    for attr in ("confidence", "gliner_confidence"):
        val = getattr(c, attr, None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _cand_pre_flagged(c: Candidate) -> bool:
    """The upstream `needs_typing` pre-flag (Pass 2 sets it for low-confidence spans).

    The GLiNER2 generator already marks borderline candidates; the router ORs that flag
    with its own coref/ellipsis/cross-turn/derives triggers so a pre-flagged span is
    never accidentally routed `direct`. Absent ⇒ False (no pre-flag, decide on triggers).
    """
    return bool(getattr(c, "needs_typing", False))


def _cand_text(c: Candidate) -> str:
    """The standalone span text the coref/ellipsis/inference heuristics run over."""
    for attr in ("fact_text", "text", "sentence"):
        val = getattr(c, attr, None)
        if isinstance(val, str) and val:
            return val
    return ""


def _cand_predicate(c: Candidate) -> str:
    val = getattr(c, "predicate", None)
    return val if isinstance(val, str) else ""


def _cand_subject_name(c: Candidate) -> Optional[str]:
    for attr in ("subject_name", "subject", "head", "head_text"):
        val = getattr(c, attr, None)
        if isinstance(val, str) and val:
            return val
    return None


def _cand_object_name(c: Candidate) -> Optional[str]:
    for attr in ("object_name", "object_literal", "object_text", "object", "tail", "tail_text"):
        val = getattr(c, attr, None)
        if isinstance(val, str) and val:
            return val
    return None


def _cand_introduced_here(c: Candidate) -> Optional[set[str]]:
    """The entity names Pass 2 says were FIRST introduced in *this* episode, if it told us.

    When the candidate carries this set we trust it; otherwise we return None and the
    caller falls back to plain `known_entities` membership (still recall-biased). This is
    optional metadata — the real Pass-2 `Candidate` may not populate it, which is fine."""
    val = getattr(c, "introduced_here", None)
    if isinstance(val, (set, frozenset)):
        return {str(x) for x in val}
    if isinstance(val, (list, tuple)):
        return {str(x) for x in val}
    return None


def _norm(name: Optional[str]) -> str:
    """Case/space-insensitive entity-name key so 'Tim Cook' == 'tim cook'."""
    return re.sub(r"\s+", " ", name.strip().lower()) if name else ""


def _record_reasons(c: Candidate, reasons: list[str]) -> None:
    """Best-effort: stamp WHY a candidate escalated onto its `escalation_reasons` field.

    Both the taxonomy `Candidate` and the typer's `Candidate` declare this field so
    Pass 4 / the ablation harness can attribute each escalation. Purely informational —
    if the candidate is frozen/slotted and won't accept the assignment we silently skip
    it (the authoritative record is always the router's own `by_reason` stats)."""
    if not hasattr(c, "escalation_reasons"):
        return
    try:
        c.escalation_reasons = tuple(reasons)  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


class RecallBiasedRouter:
    """Decide which Pass-2 candidates need Pass-4 LLM typing — and audit how often.

    Deterministic and model-free. `route()` partitions candidates into
    (to_type, direct) and updates the running escalation metrics; `last_stats`
    exposes the most recent call's {seen, escalated, rate, by_reason}, and
    `cumulative_stats` aggregates across every call on this instance.
    """

    def __init__(self, conf_threshold: float = 0.5) -> None:
        # The recall knob. Spec/GLiNER2 default candidate threshold is 0.5; anything
        # the parser was less-than-`conf_threshold` sure of is sent to the LLM. Tunable
        # so the ablation harness can sweep it against the < 20% escalation target.
        self.conf_threshold = float(conf_threshold)
        self._last = RouteStats()
        self._cum = RouteStats()

    # ── public API ──
    def route(
        self,
        candidates: Iterable[Candidate],
        known_entities: Optional[Iterable[str]] = None,
        self_entity: Optional[str] = "user",
    ) -> tuple[list[Candidate], list[Candidate]]:
        """Partition `candidates` into (to_type, direct).

        `to_type`  → escalated to the Pass-4 LLM-typing batch (hard spans).
        `direct`   → skip the LLM; cheap deterministic `asserts`/slot path.

        `known_entities` is the set of entity names already seen in PRIOR turns/sessions
        of this namespace (cross-turn edges live here — the spot deterministic isolation
        fails). It is read-only; the caller owns growing it after the episode is typed.

        `self_entity` is the namespace-owner / first-person persona name (default "user").
        It is always in `known_entities` after the first turn, but it is NOT a cross-turn
        escalation signal — first-person facts about it are trivially resolvable without
        the LLM. Passing None disables this exemption.
        """
        known = {_norm(e) for e in known_entities} if known_entities else set()
        self_key = _norm(self_entity) if self_entity else ""

        to_type: list[Candidate] = []
        direct: list[Candidate] = []
        stats = RouteStats()

        for cand in candidates:
            stats.seen += 1
            reasons = self._reasons(cand, known, self_key)
            if reasons:
                stats.escalated += 1
                for r in reasons:
                    stats.by_reason[r] = stats.by_reason.get(r, 0) + 1
                _record_reasons(cand, reasons)
                to_type.append(cand)
            else:
                direct.append(cand)

        self._last = stats
        self._accumulate(stats)
        return to_type, direct

    def should_escalate(
        self,
        candidate: Candidate,
        known_entities: Optional[Iterable[str]] = None,
        self_entity: Optional[str] = "user",
    ) -> bool:
        """Single-candidate convenience (does NOT touch the running metrics)."""
        known = {_norm(e) for e in known_entities} if known_entities else set()
        self_key = _norm(self_entity) if self_entity else ""
        return bool(self._reasons(candidate, known, self_key))

    # ── metrics surface (first-class, per spec) ──
    @property
    def last_stats(self) -> dict:
        """{seen, escalated, rate, by_reason} for the most recent route() call."""
        return self._last.as_dict()

    @property
    def cumulative_stats(self) -> dict:
        """Aggregate {seen, escalated, rate, by_reason} across all route() calls."""
        return self._cum.as_dict()

    @property
    def escalation_rate(self) -> float:
        """Cumulative escalated/seen — the < 20% BET-2 target metric."""
        return self._cum.rate

    def reset_stats(self) -> None:
        self._last = RouteStats()
        self._cum = RouteStats()

    # ── escalation logic ──
    def _reasons(self, cand: Candidate, known: set[str], self_key: str = "") -> list[str]:
        """Collect every reason this candidate must be escalated (order = check order).

        Returns [] ⇒ route direct. Multiple reasons are kept so `by_reason` reflects the
        true distribution of *why* spans escalate (a span can be both low-conf and coref)."""
        reasons: list[str] = []
        text = _cand_text(cand)

        # 0. Upstream pre-flag: Pass 2 already decided this span needs typing. OR it in
        #    so a pre-flagged candidate is never routed direct (gliner_extractor relies
        #    on the router honoring `needs_typing`).
        if _cand_pre_flagged(cand):
            reasons.append(REASON_PRE_FLAGGED)

        # 1. Low parser confidence — recall-biased: unsure ⇒ escalate.
        if _cand_confidence(cand) < self.conf_threshold:
            reasons.append(REASON_LOW_CONF)

        # 2. Coreference / ellipsis / zero-pronoun: the span is not self-contained.
        if self._has_coref_or_ellipsis(text):
            reasons.append(REASON_COREF)

        # 3. References a prior-turn/session entity not introduced in this episode.
        if self._references_prior_entity(cand, known, self_key):
            reasons.append(REASON_PRIOR_ENTITY)

        # 4. Possible `derives` (inferential) edge — LLM-only relation.
        if self._is_possible_derives(cand, text):
            reasons.append(REASON_DERIVES)

        return reasons

    @staticmethod
    def _has_coref_or_ellipsis(text: str) -> bool:
        if not text:
            return False
        if _COREF_PRONOUNS.search(text):
            return True
        # Zero-pronoun / ellipsis: a clause that *leads* with a conjunction or a bare
        # verb has dropped its subject and depends on prior context to resolve.
        if _ELLIPSIS_LEAD.match(text) or _LEADING_VERB.match(text):
            return True
        return False

    def _references_prior_entity(
        self, cand: Candidate, known: set[str], self_key: str = ""
    ) -> bool:
        """True iff the candidate touches an entity SEEN BEFORE but not introduced here.

        We check both endpoints (subject and object). If Pass 2 told us which names were
        introduced in this episode (`introduced_here`), an endpoint counts as "prior"
        only when it's in `known` AND not in that introduced set. Without that hint we
        fall back to plain `known` membership — still recall-biased.

        `self_key` (normalised) is the namespace-owner / first-person persona. It is
        always in `known` after the first turn, but it is NOT a cross-turn signal —
        first-person facts are trivially resolvable without the LLM. We skip it here so
        the 13-of-19 false escalations that drove gate-2 to 73.7% are eliminated.
        """
        if not known:
            return False
        introduced = _cand_introduced_here(cand)
        introduced_norm = {_norm(x) for x in introduced} if introduced is not None else None

        for endpoint in (_cand_subject_name(cand), _cand_object_name(cand)):
            key = _norm(endpoint)
            if not key or key not in known:
                continue
            # The self-entity (namespace owner / "user") is omnipresent across turns but
            # is never a genuine cross-turn reference — skip it.
            if self_key and key == self_key:
                continue
            if introduced_norm is not None and key in introduced_norm:
                # Re-mentioned an entity that this very episode introduced ⇒ intra-episode,
                # deterministically resolvable ⇒ not a cross-turn escalation.
                continue
            return True
        return False

    @staticmethod
    def _is_possible_derives(cand: Candidate, text: str) -> bool:
        """Heuristic for an inferential edge: an inference cue word, OR a predicate that
        isn't in our confidently-typeable set (unknown/empty ⇒ the LLM must type it)."""
        predicate = _cand_predicate(cand)
        if not predicate or predicate not in _KNOWN_PREDICATES:
            return True
        if text and _INFERENCE_CUES.search(text):
            return True
        return False

    # ── internals ──
    def _accumulate(self, stats: RouteStats) -> None:
        self._cum.seen += stats.seen
        self._cum.escalated += stats.escalated
        for reason, n in stats.by_reason.items():
            self._cum.by_reason[reason] = self._cum.by_reason.get(reason, 0) + n
