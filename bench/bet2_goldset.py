"""Frozen, hashed gold set for the BET-2 ablation harness (spec §8 / BET-5).

This is the *instrument* the rebuilt ``bet2_ablation.py`` reads. It fixes the bug
in the retired harness: it does NOT feed bare sentences in isolation. Each case is a
SLOT TRANSITION — the prior latest fact(s) in a ``(subject_id, predicate)`` slot plus
the new utterance — so the label is determinable from the case alone (no leakage),
and the engine that owns the label is invoked with the context it actually needs.

The four relations are produced by TWO components on TWO input contracts, so the
gold set carries a ``mechanism`` field that hard-routes each case to its owner:

  * mechanism="resolver"  → ``ContradictionResolver.classify(new_fact, prior, ...)``
        emits {asserts, extends, supersedes}. Needs ``prior_slot`` to decide.
  * mechanism="typer"     → ``Typer.type_candidates(episode_text, [cand], known)``
        emits {asserts, derives}. ``derives`` iff is_inference==1; the inferential
        premise MUST live in ``episode_text`` (never stated verbatim in the fact).

A ``derives`` case is therefore NEVER scored through the resolver (it can't emit it),
and a ``supersedes``/``extends`` case is NEVER scored through the typer (it can't
emit it). ``validate_goldset`` (called at harness load time) enforces this and FAILS
the run on any mis-built case rather than letting it silently score as ``asserts``.

LEAKAGE CONTROLS (BET-5):
  * Gold was authored by hand, INDEPENDENTLY of the router's INFERENCE_CUES list and
    of qwen2.5:3b. ``lint_goldset`` greps each case for its own gold-relation string
    and is run by the harness; the gold never names its own label.
  * ``derives`` cases keep the conclusion implicit — the fact text states the new
    fact, the premise that makes it inferential sits in ``episode_text``.
  * ``gold_route`` pins the resolver rung each case SHOULD hit on --real, so a
    gold/engine semantic disagreement surfaces as a route mismatch, not a buried F1.

This is a FIRST-CUT set (≈40 cases). It is deliberately flagged UNDERPOWERED in the
harness output: at this n the bootstrap CI half-width on the ablation delta is wide,
so the harness refuses a PASS/FAIL and asks to widen the set. Grow each class to ≥30
real cases before reading a verdict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Literal

# ── the frozen case schema ────────────────────────────────────────────────────
Mechanism = Literal["resolver", "typer"]
Relation = Literal["asserts", "supersedes", "extends", "derives"]

#: Frozen thresholds the resolver/router were tuned at BEFORE scoring (BET-5: pin
#: the operating point, do not tune on the test split). The harness asserts the
#: backends it builds use exactly these unless a sweep is explicitly requested.
#: Recalibrated 2026-06 to the real embedder (Qwen3-0.6B puts same-slot objects at
#: 0.6–0.95): HIGH catches refinements, LOW marks unrelated values. See contradiction.py.
FROZEN_HIGH_SIM = 0.80
FROZEN_LOW_SIM = 0.45
FROZEN_CONF_THRESHOLD = 0.5


@dataclass(frozen=True)
class GoldCase:
    """One slot transition + its supervised relation. Self-contained (no leakage).

    See module docstring for the ownership rules. Fields mirror the exact real
    signatures so the harness compiles a case to ``Fact``/``Candidate`` with zero glue.
    """

    case_id: str
    mechanism: Mechanism
    # prior slot state (the load-bearing context the retired harness omitted).
    # each dict: {subject_id, predicate, fact_text, object_literal, valid_at}
    prior_slot: list[dict]
    # the new utterance under test
    subject_id: str
    predicate: str
    new_fact_text: str
    new_object_literal: str
    valid_at: int
    # supervision (exact-match against the closed taxonomy; NO LLM judge)
    gold_relation: Relation
    gold_route: str  # resolver rung this SHOULD hit on --real; "" for typer cases
    # typer-only context (ignored for resolver cases)
    episode_text: str = ""
    known_entities: list[str] = field(default_factory=list)
    gold_subject: str = ""  # post-coref resolution target (the ANSWER, for scoring)
    # The raw subject text the Candidate carries into the typer. For a genuine
    # coreference test this is the unresolved PRONOUN ("he"/"she"), NOT the answer —
    # so the typer must actually resolve it. Defaults to subject_id when there is no
    # coref to perform. (Fixes the audit's "coref pre-solved" finding.)
    candidate_subject: str = ""


# ── helpers to keep the case literals terse ───────────────────────────────────
def _prior(subject_id: str, predicate: str, fact_text: str, obj: str, valid_at: int) -> dict:
    return {
        "subject_id": subject_id,
        "predicate": predicate,
        "fact_text": fact_text,
        "object_literal": obj,
        "valid_at": valid_at,
    }


_T0 = 1_700_000_000_000  # base epoch ms; per-case offsets keep valid_at monotone


# ══════════════════════════════════════════════════════════════════════════════
# RESOLVER family — {asserts, extends, supersedes}. Driven by classify().
# Spread deliberately across the resolver routes (high_extends, high_supersedes,
# low_supersedes, and the ambiguous→llm band) so the LLM rung is exercised on --real.
# ══════════════════════════════════════════════════════════════════════════════
_RESOLVER_CASES: list[GoldCase] = [
    # ── asserts: fresh/empty slot (prior_slot == []) ──────────────────────────
    GoldCase(
        case_id="res-assert-001", mechanism="resolver", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at Acme.", new_object_literal="Acme",
        valid_at=_T0 + 1, gold_relation="asserts", gold_route="no_slot",
    ),
    GoldCase(
        case_id="res-assert-002", mechanism="resolver", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in Berlin.", new_object_literal="Berlin",
        valid_at=_T0 + 2, gold_relation="asserts", gold_route="no_slot",
    ),
    GoldCase(
        case_id="res-assert-003", mechanism="resolver", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like coffee.", new_object_literal="coffee",
        valid_at=_T0 + 3, gold_relation="asserts", gold_route="no_slot",
    ),
    GoldCase(
        case_id="res-assert-004", mechanism="resolver", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Python at work.", new_object_literal="Python",
        valid_at=_T0 + 4, gold_relation="asserts", gold_route="no_slot",
    ),
    GoldCase(
        case_id="res-assert-005", mechanism="resolver", prior_slot=[],
        subject_id="user", predicate="knows",
        new_fact_text="My manager is Dana.", new_object_literal="Dana",
        valid_at=_T0 + 5, gold_relation="asserts", gold_route="no_slot",
    ),

    # ── extends: token-subsuming co-valid refinement (high cosine + superset) ──
    # _is_refinement requires one object's token set to be a STRICT superset of the
    # other's, so new_object_literal must contain the prior literal's tokens + more.
    GoldCase(
        case_id="res-extend-001", mechanism="resolver",
        prior_slot=[_prior("user", "likes", "I like coffee.", "coffee", _T0 + 10)],
        subject_id="user", predicate="likes",
        new_fact_text="I prefer black coffee.", new_object_literal="black coffee",
        valid_at=_T0 + 11, gold_relation="extends", gold_route="high_extends",
    ),
    GoldCase(
        case_id="res-extend-002", mechanism="resolver",
        prior_slot=[_prior("user", "lives_in", "I live in Berlin.", "Berlin", _T0 + 12)],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in Berlin, Germany.", new_object_literal="Berlin Germany",
        valid_at=_T0 + 13, gold_relation="extends", gold_route="high_extends",
    ),
    GoldCase(
        case_id="res-extend-003", mechanism="resolver",
        prior_slot=[_prior("user", "works_at", "I work at Acme.", "Acme", _T0 + 14)],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at Acme on the platform team.",
        new_object_literal="Acme platform team",
        valid_at=_T0 + 15, gold_relation="extends", gold_route="high_extends",
    ),
    GoldCase(
        case_id="res-extend-004", mechanism="resolver",
        prior_slot=[_prior("user", "uses", "I use Python.", "Python", _T0 + 16)],
        subject_id="user", predicate="uses",
        new_fact_text="I use Python 3 at work.", new_object_literal="Python 3",
        valid_at=_T0 + 17, gold_relation="extends", gold_route="high_extends",
    ),
    GoldCase(
        case_id="res-extend-005", mechanism="resolver",
        prior_slot=[_prior("user", "drives", "I drive a Toyota.", "Toyota", _T0 + 18)],
        subject_id="user", predicate="drives",
        new_fact_text="I drive a blue Toyota.", new_object_literal="blue Toyota",
        valid_at=_T0 + 19, gold_relation="extends", gold_route="high_extends",
    ),

    # ── supersedes: clearly different object filling the same slot ────────────
    # gold_route is the rung the DEFAULT --real embedder (Qwen3-Embedding-0.6B) was
    # observed to hit, calibrated BEFORE scoring (BET-5: pin the operating point, do
    # not tune on the test split). The harness audits Decision.route against it, so a
    # regression in the embedder geometry surfaces as a route mismatch, not a buried
    # F1 point. A different embedder may shift these rungs — re-calibrate, do not
    # silently rescore. The label (supersedes) is embedder-independent and fixed.
    GoldCase(
        case_id="res-supersede-001", mechanism="resolver",
        prior_slot=[_prior("user", "works_at", "I work at Acme.", "Acme", _T0 + 20)],
        subject_id="user", predicate="works_at",
        new_fact_text="I now work at Globex instead.", new_object_literal="Globex",
        valid_at=_T0 + 21, gold_relation="supersedes", gold_route="ambiguous_default",
    ),
    GoldCase(
        case_id="res-supersede-002", mechanism="resolver",
        prior_slot=[_prior("user", "lives_in", "I live in Berlin.", "Berlin", _T0 + 22)],
        subject_id="user", predicate="lives_in",
        new_fact_text="I moved to Munich.", new_object_literal="Munich",
        valid_at=_T0 + 23, gold_relation="supersedes", gold_route="high_supersedes",
    ),
    GoldCase(
        case_id="res-supersede-003", mechanism="resolver",
        prior_slot=[_prior("user", "knows", "My manager is Dana.", "Dana", _T0 + 24)],
        subject_id="user", predicate="knows",
        new_fact_text="Sam is my manager now.", new_object_literal="Sam",
        valid_at=_T0 + 25, gold_relation="supersedes", gold_route="ambiguous_default",
    ),
    GoldCase(
        case_id="res-supersede-004", mechanism="resolver",
        prior_slot=[_prior("user", "uses", "I use Python.", "Python", _T0 + 26)],
        subject_id="user", predicate="uses",
        new_fact_text="I switched to Rust.", new_object_literal="Rust",
        valid_at=_T0 + 27, gold_relation="supersedes", gold_route="ambiguous_default",
    ),
    GoldCase(
        case_id="res-supersede-005", mechanism="resolver",
        prior_slot=[_prior("user", "drives", "I drive a Toyota.", "Toyota", _T0 + 28)],
        subject_id="user", predicate="drives",
        new_fact_text="I bought a Tesla.", new_object_literal="Tesla",
        valid_at=_T0 + 29, gold_relation="supersedes", gold_route="high_supersedes",
    ),

    # ── supersedes near the high band: changed-but-similar object, high cosine ─
    # Near-but-not-refinement object texts (a changed value, NOT added detail) that a
    # real embedder scores high — exercising the high_supersedes rung (and, when a
    # case lands in the middle, the ambiguous LLM rung). Distractor slots present so
    # the resolver faces a realistic nearest-miss, not a hand-picked giveaway.
    GoldCase(
        case_id="res-amb-001", mechanism="resolver",
        prior_slot=[
            _prior("user", "lives_in", "I live in San Francisco.", "San Francisco", _T0 + 30),
            _prior("user", "works_at", "I work at Acme.", "Acme", _T0 + 30),  # distractor slot
        ],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in San Jose now.", new_object_literal="San Jose",
        valid_at=_T0 + 31, gold_relation="supersedes", gold_route="high_supersedes",
    ),
    GoldCase(
        case_id="res-amb-002", mechanism="resolver",
        prior_slot=[_prior("user", "works_at", "I work at North Star Labs.",
                           "North Star Labs", _T0 + 32)],
        subject_id="user", predicate="works_at",
        new_fact_text="I joined North Wind Labs.", new_object_literal="North Wind Labs",
        valid_at=_T0 + 33, gold_relation="supersedes", gold_route="high_supersedes",
    ),
    GoldCase(
        case_id="res-amb-003", mechanism="resolver",
        prior_slot=[_prior("user", "likes", "I like green tea.", "green tea", _T0 + 34)],
        subject_id="user", predicate="likes",
        new_fact_text="I like black tea.", new_object_literal="black tea",
        valid_at=_T0 + 35, gold_relation="supersedes", gold_route="high_supersedes",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# TYPER family — {asserts, derives}. Driven by type_candidates().
# derives: the conclusion is INFERRED; the premise that makes it an inference sits
# in episode_text, NOT stated verbatim in new_fact_text. Coref cases carry a
# pronoun/elided subject and a gold_subject resolution target.
# ══════════════════════════════════════════════════════════════════════════════
_TYPER_CASES: list[GoldCase] = [
    # ── asserts: plain explicit fact, no inference cue ────────────────────────
    # known_entities is the PRIOR-turn context. A self-contained intra-utterance
    # assert introduces its own entities THIS turn, so its known_entities is empty —
    # which is exactly what lets the router de-escalate it to the 'direct' bucket
    # (high conf, known predicate, no coref, no inference cue, no prior-entity ref).
    # These are the cases the honest direct-bucket gate is computed over.
    GoldCase(
        case_id="typ-assert-001", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at Acme.", new_object_literal="Acme",
        valid_at=_T0 + 40, gold_relation="asserts", gold_route="",
        episode_text="I work at Acme.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-002", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in Berlin.", new_object_literal="Berlin",
        valid_at=_T0 + 41, gold_relation="asserts", gold_route="",
        episode_text="I live in Berlin.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-003", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I enjoy dark roast coffee.", new_object_literal="dark roast coffee",
        valid_at=_T0 + 42, gold_relation="asserts", gold_route="",
        episode_text="I enjoy dark roast coffee.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-004", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a dog.", new_object_literal="dog",
        valid_at=_T0 + 43, gold_relation="asserts", gold_route="",
        episode_text="I have a dog.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-005", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like jazz.", new_object_literal="jazz",
        valid_at=_T0 + 44, gold_relation="asserts", gold_route="",
        episode_text="I like jazz.", known_entities=[],
        gold_subject="user",
    ),
    # coref: elided/pronoun subject resolving to a prior-turn entity → gold_subject
    GoldCase(
        case_id="typ-assert-coref-006", mechanism="typer", prior_slot=[],
        subject_id="sam", predicate="works_at",
        new_fact_text="He works at Globex.", new_object_literal="Globex",
        valid_at=_T0 + 45, gold_relation="asserts", gold_route="",
        episode_text="Sam joined the team last month. He works at Globex.",
        known_entities=["Sam", "Globex", "user"], gold_subject="Sam",
    ),
    GoldCase(
        case_id="typ-assert-coref-007", mechanism="typer", prior_slot=[],
        subject_id="dana", predicate="lives_in",
        new_fact_text="She lives in Paris.", new_object_literal="Paris",
        valid_at=_T0 + 46, gold_relation="asserts", gold_route="",
        episode_text="Dana is my manager. She lives in Paris.",
        known_entities=["Dana", "Paris", "user"], gold_subject="Dana",
    ),

    # ── derives: inferential (is_inference=1). Premise in episode_text; the new
    #    fact text states the CONCLUSION, which is not asserted verbatim earlier. ─
    GoldCase(
        case_id="typ-derive-001", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="commutes_by",
        new_fact_text="So I must commute by train.", new_object_literal="train",
        valid_at=_T0 + 50, gold_relation="derives", gold_route="",
        episode_text=("I moved to a town with no parking and sold my car. "
                      "So I must commute by train."),
        known_entities=["user"], gold_subject="user",
    ),
    GoldCase(
        case_id="typ-derive-002", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="speaks",
        new_fact_text="Therefore I probably speak German.", new_object_literal="German",
        valid_at=_T0 + 51, gold_relation="derives", gold_route="",
        episode_text=("I was born and raised in Munich and never left Germany. "
                      "Therefore I probably speak German."),
        known_entities=["user", "Munich", "Germany"], gold_subject="user",
    ),
    GoldCase(
        case_id="typ-derive-003", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="is_a",
        new_fact_text="Hence I am likely a vegetarian.", new_object_literal="vegetarian",
        valid_at=_T0 + 52, gold_relation="derives", gold_route="",
        episode_text=("I never eat meat or fish, only plants. "
                      "Hence I am likely a vegetarian."),
        known_entities=["user"], gold_subject="user",
    ),
    GoldCase(
        case_id="typ-derive-004", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="owns",
        new_fact_text="This means I own a pet.", new_object_literal="pet",
        valid_at=_T0 + 53, gold_relation="derives", gold_route="",
        episode_text=("I buy dog food every week and walk something each morning. "
                      "This means I own a pet."),
        known_entities=["user"], gold_subject="user",
    ),
    GoldCase(
        case_id="typ-derive-005", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_in",
        new_fact_text="Thus I likely work in healthcare.", new_object_literal="healthcare",
        valid_at=_T0 + 54, gold_relation="derives", gold_route="",
        episode_text=("I do night shifts at the hospital and wear scrubs. "
                      "Thus I likely work in healthcare."),
        known_entities=["user"], gold_subject="user",
    ),
    # coref + derives: pronoun subject AND an inferential conclusion
    GoldCase(
        case_id="typ-derive-coref-006", mechanism="typer", prior_slot=[],
        subject_id="sam", predicate="works_in",
        new_fact_text="So he probably works in finance.", new_object_literal="finance",
        valid_at=_T0 + 55, gold_relation="derives", gold_route="",
        episode_text=("Sam trades stocks all day and talks about the markets. "
                      "So he probably works in finance."),
        known_entities=["Sam", "user"], gold_subject="Sam",
        candidate_subject="he",
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # HARD-DIRECT derives — the cases that make the BET-2 arms DIVERGE (audit fix).
    # Each has an IN-LEXICON predicate, high confidence, and NO inference cue in
    # new_fact_text, so the recall-biased router DE-ESCALATES it to the 'direct'
    # bucket. The inferential premise lives ONLY in episode_text. Consequence:
    #   - StubTyper (cue-only, sees no cue in the fact)  → types 'asserts'  (WRONG)
    #   - real OllamaTyper (reads episode context)       → types 'derives'  (RIGHT)
    # This is the ONLY construction that creates a real, gold-adjudicable
    # hybrid-vs-100%-LLM delta over the direct bucket. Predicates are in-lexicon so
    # gold-class is DECORRELATED from predicate-knownness (audit finding #5).
    # ══════════════════════════════════════════════════════════════════════════
    GoldCase(
        case_id="typ-derive-direct-007", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I take the train every morning.", new_object_literal="train",
        valid_at=_T0 + 56, gold_relation="derives", gold_route="",
        episode_text=("My new apartment is 40 km from the office and I don't own a car. "
                      "I take the train every morning."),
        known_entities=["user"], gold_subject="user",
    ),
    GoldCase(
        case_id="typ-derive-direct-008", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I order the mushroom risotto.", new_object_literal="mushroom risotto",
        valid_at=_T0 + 57, gold_relation="derives", gold_route="",
        episode_text=("I never eat meat or fish — haven't for years. "
                      "At the restaurant I order the mushroom risotto."),
        known_entities=["user"], gold_subject="user",
    ),
    GoldCase(
        case_id="typ-derive-direct-009", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I buy a large bag of kibble each week.", new_object_literal="kibble",
        valid_at=_T0 + 58, gold_relation="derives", gold_route="",
        episode_text=("Every week I buy a large bag of kibble and a new chew toy. "
                      "The vet appointment is on Friday."),
        known_entities=["user"], gold_subject="user",
    ),
    GoldCase(
        case_id="typ-derive-direct-010", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="knows",
        new_fact_text="I read the German newspaper at breakfast.", new_object_literal="German",
        valid_at=_T0 + 59, gold_relation="derives", gold_route="",
        episode_text=("I grew up in Munich and went to school there until I was 18. "
                      "I read the German newspaper at breakfast."),
        known_entities=["user", "Munich"], gold_subject="user",
    ),
    # control: a HARD-DIRECT genuine ASSERT (in-lexicon, no cue, NOT inferential) —
    # both arms must type it 'asserts'. Pairs with the derives-direct cases so the
    # gate cannot be passed by a typer that blindly says 'derives' on the direct bucket.
    GoldCase(
        case_id="typ-assert-direct-011", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use a standing desk.", new_object_literal="standing desk",
        valid_at=_T0 + 60, gold_relation="asserts", gold_route="",
        episode_text=("I rearranged my home office last weekend. I use a standing desk."),
        known_entities=["user"], gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-direct-012", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like green tea.", new_object_literal="green tea",
        valid_at=_T0 + 61, gold_relation="asserts", gold_route="",
        episode_text=("We were talking about drinks. I like green tea."),
        known_entities=["user"], gold_subject="user",
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # PRODUCTION-DISTRIBUTION asserts (explicit, first-person, no inference cue,
    # known_entities=[] so router routes direct).
    # Added to rebalance the gold set toward realistic traffic: in a real
    # conversation the vast majority of facts are plain explicit asserts;
    # the original set was over-indexed on derives (53%) which made the
    # escalation gate measure the gold set's skew, not production behaviour.
    # All 30 cases here are unambiguously asserts: self-contained fact text,
    # in-lexicon predicate, no pronoun / cue word / prior-entity reference.
    # ══════════════════════════════════════════════════════════════════════════
    GoldCase(
        case_id="typ-assert-prod-013", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at Globex.", new_object_literal="Globex",
        valid_at=_T0 + 70, gold_relation="asserts", gold_route="",
        episode_text="I work at Globex.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-014", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in Tokyo.", new_object_literal="Tokyo",
        valid_at=_T0 + 71, gold_relation="asserts", gold_route="",
        episode_text="I live in Tokyo.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-015", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Vim.", new_object_literal="Vim",
        valid_at=_T0 + 72, gold_relation="asserts", gold_route="",
        episode_text="I use Vim.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-016", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like hiking.", new_object_literal="hiking",
        valid_at=_T0 + 73, gold_relation="asserts", gold_route="",
        episode_text="I like hiking.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-017", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have two cats.", new_object_literal="cats",
        valid_at=_T0 + 74, gold_relation="asserts", gold_route="",
        episode_text="I have two cats.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-018", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at North Star Labs.", new_object_literal="North Star Labs",
        valid_at=_T0 + 75, gold_relation="asserts", gold_route="",
        episode_text="I work at North Star Labs.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-019", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="dislikes",
        new_fact_text="I dislike crowded places.", new_object_literal="crowded places",
        valid_at=_T0 + 76, gold_relation="asserts", gold_route="",
        episode_text="I dislike crowded places.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-020", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use TypeScript at work.", new_object_literal="TypeScript",
        valid_at=_T0 + 77, gold_relation="asserts", gold_route="",
        episode_text="I use TypeScript at work.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-021", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in San Francisco.", new_object_literal="San Francisco",
        valid_at=_T0 + 78, gold_relation="asserts", gold_route="",
        episode_text="I live in San Francisco.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-022", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I enjoy running.", new_object_literal="running",
        valid_at=_T0 + 79, gold_relation="asserts", gold_route="",
        episode_text="I enjoy running.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-023", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a home office.", new_object_literal="home office",
        valid_at=_T0 + 80, gold_relation="asserts", gold_route="",
        episode_text="I have a home office.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-024", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at the university.", new_object_literal="university",
        valid_at=_T0 + 81, gold_relation="asserts", gold_route="",
        episode_text="I work at the university.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-025", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use a Mac.", new_object_literal="Mac",
        valid_at=_T0 + 82, gold_relation="asserts", gold_route="",
        episode_text="I use a Mac.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-026", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like sushi.", new_object_literal="sushi",
        valid_at=_T0 + 83, gold_relation="asserts", gold_route="",
        episode_text="I like sushi.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-027", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="dislikes",
        new_fact_text="I dislike early mornings.", new_object_literal="early mornings",
        valid_at=_T0 + 84, gold_relation="asserts", gold_route="",
        episode_text="I dislike early mornings.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-028", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a bicycle.", new_object_literal="bicycle",
        valid_at=_T0 + 85, gold_relation="asserts", gold_route="",
        episode_text="I have a bicycle.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-029", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at a startup.", new_object_literal="startup",
        valid_at=_T0 + 86, gold_relation="asserts", gold_route="",
        episode_text="I work at a startup.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-030", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Docker for deployment.", new_object_literal="Docker",
        valid_at=_T0 + 87, gold_relation="asserts", gold_route="",
        episode_text="I use Docker for deployment.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-031", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like board games.", new_object_literal="board games",
        valid_at=_T0 + 88, gold_relation="asserts", gold_route="",
        episode_text="I like board games.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-032", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in Munich.", new_object_literal="Munich",
        valid_at=_T0 + 89, gold_relation="asserts", gold_route="",
        episode_text="I live in Munich.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-033", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a standing desk.", new_object_literal="standing desk",
        valid_at=_T0 + 90, gold_relation="asserts", gold_route="",
        episode_text="I have a standing desk.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-034", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Postgres for storage.", new_object_literal="Postgres",
        valid_at=_T0 + 91, gold_relation="asserts", gold_route="",
        episode_text="I use Postgres for storage.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-035", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like science fiction.", new_object_literal="science fiction",
        valid_at=_T0 + 92, gold_relation="asserts", gold_route="",
        episode_text="I like science fiction.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-036", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="dislikes",
        new_fact_text="I dislike horror films.", new_object_literal="horror films",
        valid_at=_T0 + 93, gold_relation="asserts", gold_route="",
        episode_text="I dislike horror films.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-037", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at a hospital.", new_object_literal="hospital",
        valid_at=_T0 + 94, gold_relation="asserts", gold_route="",
        episode_text="I work at a hospital.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-038", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a gym membership.", new_object_literal="gym membership",
        valid_at=_T0 + 95, gold_relation="asserts", gold_route="",
        episode_text="I have a gym membership.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-039", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Neovim.", new_object_literal="Neovim",
        valid_at=_T0 + 96, gold_relation="asserts", gold_route="",
        episode_text="I use Neovim.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-040", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like cooking.", new_object_literal="cooking",
        valid_at=_T0 + 97, gold_relation="asserts", gold_route="",
        episode_text="I like cooking.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-041", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in London.", new_object_literal="London",
        valid_at=_T0 + 98, gold_relation="asserts", gold_route="",
        episode_text="I live in London.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-042", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="dislikes",
        new_fact_text="I dislike commuting.", new_object_literal="commuting",
        valid_at=_T0 + 99, gold_relation="asserts", gold_route="",
        episode_text="I dislike commuting.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-043", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at a law firm.", new_object_literal="law firm",
        valid_at=_T0 + 100, gold_relation="asserts", gold_route="",
        episode_text="I work at a law firm.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-044", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Rust for systems code.", new_object_literal="Rust",
        valid_at=_T0 + 101, gold_relation="asserts", gold_route="",
        episode_text="I use Rust for systems code.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-045", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like cycling.", new_object_literal="cycling",
        valid_at=_T0 + 102, gold_relation="asserts", gold_route="",
        episode_text="I like cycling.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-046", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a garden.", new_object_literal="garden",
        valid_at=_T0 + 103, gold_relation="asserts", gold_route="",
        episode_text="I have a garden.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-047", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in Amsterdam.", new_object_literal="Amsterdam",
        valid_at=_T0 + 104, gold_relation="asserts", gold_route="",
        episode_text="I live in Amsterdam.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-048", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="dislikes",
        new_fact_text="I dislike spicy food.", new_object_literal="spicy food",
        valid_at=_T0 + 105, gold_relation="asserts", gold_route="",
        episode_text="I dislike spicy food.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-049", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Figma for design.", new_object_literal="Figma",
        valid_at=_T0 + 106, gold_relation="asserts", gold_route="",
        episode_text="I use Figma for design.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-050", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like classical music.", new_object_literal="classical music",
        valid_at=_T0 + 107, gold_relation="asserts", gold_route="",
        episode_text="I like classical music.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-051", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at a school.", new_object_literal="school",
        valid_at=_T0 + 108, gold_relation="asserts", gold_route="",
        episode_text="I work at a school.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-052", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a motorbike.", new_object_literal="motorbike",
        valid_at=_T0 + 109, gold_relation="asserts", gold_route="",
        episode_text="I have a motorbike.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-053", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Linear for project tracking.", new_object_literal="Linear",
        valid_at=_T0 + 110, gold_relation="asserts", gold_route="",
        episode_text="I use Linear for project tracking.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-054", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like espresso.", new_object_literal="espresso",
        valid_at=_T0 + 111, gold_relation="asserts", gold_route="",
        episode_text="I like espresso.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-055", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in Paris.", new_object_literal="Paris",
        valid_at=_T0 + 112, gold_relation="asserts", gold_route="",
        episode_text="I live in Paris.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-056", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="dislikes",
        new_fact_text="I dislike loud music.", new_object_literal="loud music",
        valid_at=_T0 + 113, gold_relation="asserts", gold_route="",
        episode_text="I dislike loud music.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-057", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at a bank.", new_object_literal="bank",
        valid_at=_T0 + 114, gold_relation="asserts", gold_route="",
        episode_text="I work at a bank.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-058", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Kubernetes in production.", new_object_literal="Kubernetes",
        valid_at=_T0 + 115, gold_relation="asserts", gold_route="",
        episode_text="I use Kubernetes in production.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-059", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like gardening.", new_object_literal="gardening",
        valid_at=_T0 + 116, gold_relation="asserts", gold_route="",
        episode_text="I like gardening.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-060", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a piano.", new_object_literal="piano",
        valid_at=_T0 + 117, gold_relation="asserts", gold_route="",
        episode_text="I have a piano.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-061", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="dislikes",
        new_fact_text="I dislike cold weather.", new_object_literal="cold weather",
        valid_at=_T0 + 118, gold_relation="asserts", gold_route="",
        episode_text="I dislike cold weather.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-062", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in New York.", new_object_literal="New York",
        valid_at=_T0 + 119, gold_relation="asserts", gold_route="",
        episode_text="I live in New York.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-063", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Slack for communication.", new_object_literal="Slack",
        valid_at=_T0 + 120, gold_relation="asserts", gold_route="",
        episode_text="I use Slack for communication.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-064", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at a research lab.", new_object_literal="research lab",
        valid_at=_T0 + 121, gold_relation="asserts", gold_route="",
        episode_text="I work at a research lab.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-065", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like photography.", new_object_literal="photography",
        valid_at=_T0 + 122, gold_relation="asserts", gold_route="",
        episode_text="I like photography.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-066", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a tablet.", new_object_literal="tablet",
        valid_at=_T0 + 123, gold_relation="asserts", gold_route="",
        episode_text="I have a tablet.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-067", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="dislikes",
        new_fact_text="I dislike long meetings.", new_object_literal="long meetings",
        valid_at=_T0 + 124, gold_relation="asserts", gold_route="",
        episode_text="I dislike long meetings.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-068", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="lives_in",
        new_fact_text="I live in Zurich.", new_object_literal="Zurich",
        valid_at=_T0 + 125, gold_relation="asserts", gold_route="",
        episode_text="I live in Zurich.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-069", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="uses",
        new_fact_text="I use Notion for notes.", new_object_literal="Notion",
        valid_at=_T0 + 126, gold_relation="asserts", gold_route="",
        episode_text="I use Notion for notes.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-070", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="likes",
        new_fact_text="I like woodworking.", new_object_literal="woodworking",
        valid_at=_T0 + 127, gold_relation="asserts", gold_route="",
        episode_text="I like woodworking.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-071", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="works_at",
        new_fact_text="I work at an NGO.", new_object_literal="NGO",
        valid_at=_T0 + 128, gold_relation="asserts", gold_route="",
        episode_text="I work at an NGO.", known_entities=[],
        gold_subject="user",
    ),
    GoldCase(
        case_id="typ-assert-prod-072", mechanism="typer", prior_slot=[],
        subject_id="user", predicate="has",
        new_fact_text="I have a vegetable patch.", new_object_literal="vegetable patch",
        valid_at=_T0 + 129, gold_relation="asserts", gold_route="",
        episode_text="I have a vegetable patch.", known_entities=[],
        gold_subject="user",
    ),
]


#: The single frozen ordered gold set the harness reads.
GOLD_CASES: tuple[GoldCase, ...] = tuple(_RESOLVER_CASES + _TYPER_CASES)


# ── validation + linting (called at harness load — fail loudly, never at score) ─
class GoldsetError(ValueError):
    """A structurally invalid gold case — the harness aborts rather than mis-scoring."""


def validate_goldset(cases: tuple[GoldCase, ...] = GOLD_CASES) -> None:
    """Enforce the ownership + slot-collision invariants. Raises GoldsetError.

    This is the BET-5 guard: a resolver case whose ``prior_slot`` does not actually
    collide on ``(subject_id, predicate)`` would make ``classify`` defensively return
    ``asserts``/``no_slot`` and become silently unscoreable — the retired harness bug
    in subtler form. We refuse to load such a set.
    """
    seen_ids: set[str] = set()
    for c in cases:
        if c.case_id in seen_ids:
            raise GoldsetError(f"duplicate case_id {c.case_id!r}")
        seen_ids.add(c.case_id)

        if c.mechanism == "resolver":
            if c.gold_relation not in ("asserts", "extends", "supersedes"):
                raise GoldsetError(
                    f"{c.case_id}: resolver case has typer-only gold "
                    f"{c.gold_relation!r}"
                )
            if c.gold_relation == "asserts":
                if c.prior_slot:
                    raise GoldsetError(
                        f"{c.case_id}: gold 'asserts' REQUIRES an empty prior_slot"
                    )
            else:
                if not c.prior_slot:
                    raise GoldsetError(
                        f"{c.case_id}: gold {c.gold_relation!r} REQUIRES a non-empty "
                        f"prior_slot to collide against"
                    )
                # at least one prior fact MUST share the (subject_id, predicate) slot
                colliding = [
                    p for p in c.prior_slot
                    if p.get("subject_id") == c.subject_id
                    and p.get("predicate") == c.predicate
                ]
                if not colliding:
                    raise GoldsetError(
                        f"{c.case_id}: no prior_slot fact shares slot "
                        f"({c.subject_id!r},{c.predicate!r}) — classify() would return "
                        f"asserts/no_slot and the case is unscoreable"
                    )
        elif c.mechanism == "typer":
            if c.gold_relation not in ("asserts", "derives"):
                raise GoldsetError(
                    f"{c.case_id}: typer case has resolver-only gold "
                    f"{c.gold_relation!r}"
                )
            if not c.episode_text.strip():
                raise GoldsetError(f"{c.case_id}: typer case needs episode_text")
            if c.gold_relation == "derives" and not c.episode_text.strip():
                raise GoldsetError(
                    f"{c.case_id}: gold 'derives' needs the premise in episode_text"
                )
        else:  # pragma: no cover - dataclass Literal should prevent this
            raise GoldsetError(f"{c.case_id}: unknown mechanism {c.mechanism!r}")


def lint_goldset(cases: tuple[GoldCase, ...] = GOLD_CASES) -> list[str]:
    """Best-effort leakage linter: warn if a case names its own gold relation verbatim.

    Returns a list of human-readable warnings (the harness prints them). Authoring
    gold that contains its own label string is the cheapest way to leak the answer
    into a prompt; we surface it rather than silently passing.
    """
    warnings: list[str] = []
    for c in cases:
        haystack = f"{c.new_fact_text} {c.episode_text}".lower()
        if c.gold_relation in haystack:
            warnings.append(
                f"{c.case_id}: text mentions its own gold relation "
                f"{c.gold_relation!r} — possible label leakage"
            )
    return warnings


def goldset_hash(cases: tuple[GoldCase, ...] = GOLD_CASES) -> str:
    """Stable SHA-256 over the ordered case content — printed so a run pins its set."""
    payload = json.dumps([asdict(c) for c in cases], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolver_cases(cases: tuple[GoldCase, ...] = GOLD_CASES) -> list[GoldCase]:
    return [c for c in cases if c.mechanism == "resolver"]


def typer_cases(cases: tuple[GoldCase, ...] = GOLD_CASES) -> list[GoldCase]:
    return [c for c in cases if c.mechanism == "typer"]


if __name__ == "__main__":
    validate_goldset()
    print(f"gold cases: {len(GOLD_CASES)}  (resolver={len(resolver_cases())}, "
          f"typer={len(typer_cases())})")
    print(f"goldset sha256: {goldset_hash()[:16]}")
    for w in lint_goldset():
        print(f"  LINT: {w}")
