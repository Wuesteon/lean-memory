"""Regression tests for the recall-biased router (Pass 3).

Focus: the BET-2 gate-2 constraint — escalation rate < 20% on realistic first-person
episodes where "user" is a known entity. The primary regression being guarded is the
prior_entity trigger firing on the self-entity ("user"), which drove escalation to 73.7%
before the self_entity exemption was added.
"""

from __future__ import annotations

import pytest

from lean_memory.extract.router import RecallBiasedRouter, REASON_PRIOR_ENTITY
from lean_memory.extract.taxonomy import Candidate

_T0 = 1_700_000_000_000


def _cand(
    subject: str,
    predicate: str,
    obj: str,
    text: str,
    *,
    confidence: float = 0.9,
) -> Candidate:
    return Candidate(
        subject_name=subject,
        predicate=predicate,
        object_literal=obj,
        fact_text=text,
        valid_at=_T0,
        confidence=confidence,
        source="stub",
    )


class TestSelfEntityExemption:
    """The self-entity ("user") must NOT trigger prior_entity escalation."""

    def test_first_person_facts_not_escalated_by_prior_entity(self) -> None:
        """Core BET-2 regression: first-person facts about "user" route direct
        even when "user" is in known_entities from prior turns."""
        router = RecallBiasedRouter(conf_threshold=0.5)
        candidates = [
            _cand("user", "works_at", "Acme", "I work at Acme."),
            _cand("user", "lives_in", "Berlin", "I live in Berlin."),
            _cand("user", "likes", "coffee", "I like coffee."),
            _cand("user", "uses", "Python", "I use Python."),
            _cand("user", "has", "dog", "I have a dog."),
        ]
        # "user" is in known_entities — simulates a second+ turn
        to_type, direct = router.route(
            candidates,
            known_entities=["user"],
            self_entity="user",
        )
        # None should escalate on prior_entity; all are high-confidence, no coref, known predicates
        prior_entity_reasons = [
            r
            for r in router.last_stats["by_reason"]
            if r == REASON_PRIOR_ENTITY
        ]
        assert not prior_entity_reasons, (
            f"prior_entity escalated {router.last_stats['by_reason'].get(REASON_PRIOR_ENTITY, 0)} "
            f"first-person candidates — self_entity exemption is broken"
        )

    def test_escalation_rate_under_gate_on_first_person_episode(self) -> None:
        """Escalation rate must stay <20% for a realistic first-person episode
        where all facts are explicit, high-confidence, and known-predicate."""
        router = RecallBiasedRouter(conf_threshold=0.5)
        candidates = [
            _cand("user", "works_at", "Acme", "I work at Acme."),
            _cand("user", "lives_in", "Berlin", "I live in Berlin."),
            _cand("user", "likes", "jazz", "I like jazz."),
            _cand("user", "uses", "Python", "I use Python."),
            _cand("user", "has", "dog", "I have a dog."),
            _cand("user", "likes", "coffee", "I like coffee."),
        ]
        router.route(candidates, known_entities=["user"], self_entity="user")
        assert router.last_stats["rate"] < 0.20, (
            f"escalation rate {router.last_stats['rate']:.1%} exceeds BET-2 gate (<20%) "
            f"on a pure first-person explicit episode"
        )

    def test_third_party_entity_still_escalates(self) -> None:
        """A fact referencing a PRIOR third-party entity (not "user") must still escalate."""
        router = RecallBiasedRouter(conf_threshold=0.5)
        # "Sam" is a prior entity; a fact about Sam referencing a prior entity should escalate
        cand = _cand("Sam", "works_at", "Globex", "Sam works at Globex.")
        to_type, direct = router.route(
            [cand],
            known_entities=["Sam", "user"],
            self_entity="user",
        )
        assert cand in to_type, (
            "A fact about a prior third-party entity ('Sam') should escalate via prior_entity"
        )

    def test_self_entity_none_restores_original_behaviour(self) -> None:
        """With self_entity=None the exemption is disabled — "user" escalates again."""
        router = RecallBiasedRouter(conf_threshold=0.5)
        cand = _cand("user", "works_at", "Acme", "I work at Acme.")
        to_type, direct = router.route(
            [cand],
            known_entities=["user"],
            self_entity=None,
        )
        assert cand in to_type, (
            "With self_entity=None the prior_entity trigger should fire on 'user'"
        )


class TestKnownPredicatesExpanded:
    """Predicates added in the BET-2 fix must not trigger spurious derives escalation."""

    @pytest.mark.parametrize("predicate", [
        "owns", "member_of", "drives", "speaks", "plays",
        "skilled_in", "interested_in", "works_in", "commutes_by",
    ])
    def test_expanded_predicates_do_not_escalate_as_derives(self, predicate: str) -> None:
        router = RecallBiasedRouter(conf_threshold=0.5)
        cand = _cand("user", predicate, "something", f"I {predicate} something.")
        to_type, direct = router.route([cand], known_entities=[], self_entity="user")
        from lean_memory.extract.router import REASON_DERIVES
        reasons = router.last_stats["by_reason"]
        assert reasons.get(REASON_DERIVES, 0) == 0, (
            f"predicate {predicate!r} escalated as derives but it is an explicit known predicate"
        )


class TestInferentialEscalation:
    """Genuinely inferential cues must still escalate even after the fix."""

    def test_inference_cue_still_escalates(self) -> None:
        router = RecallBiasedRouter(conf_threshold=0.5)
        cand = _cand("user", "commutes_by", "train",
                     "I must therefore commute by train.")
        to_type, direct = router.route([cand], known_entities=[], self_entity="user")
        assert cand in to_type, "Inference cue word should still trigger escalation"

    def test_pronoun_coref_still_escalates(self) -> None:
        router = RecallBiasedRouter(conf_threshold=0.5)
        # Endpoint-scoped contract (2026-07 recalibration, Task 4): the subject
        # endpoint "She" is a pronoun, so this escalates as coreference. (Previously
        # relied on the mid-text pronoun scan; now the endpoint itself carries it.)
        cand = _cand("She", "lives_in", "Paris", "She lives in Paris.")
        to_type, direct = router.route([cand], known_entities=["Sam"], self_entity="user")
        assert cand in to_type, "Pronoun coreference should still escalate"


# ── Task 4: endpoint-scoped coref/ellipsis (2026-07 recalibration) ────────────
# The old router escalated on ANY pronoun/demonstrative anywhere in fact_text,
# which fired on 65.6% of real conversational candidates (2026-07 baseline probe)
# — conversational filler, not an unresolvable reference. The new contract only
# escalates when the candidate's OWN endpoints are ungrounded.


def _grounded_cand(subject="Alice", obj="Acme", predicate="works_at",
                   text=None, conf=0.9, subject_span=(0, 5), object_span=(15, 19)):
    return Candidate(
        subject_name=subject, predicate=predicate, object_literal=obj,
        fact_text=text or f"{subject} works at {obj}.", valid_at=0,
        confidence=conf, source="test",
        subject_span=subject_span, object_span=object_span, needs_typing=False,
    )


def test_grounded_endpoints_with_stray_pronoun_route_direct():
    """Conversational filler ('that', 'it', 'there') must not escalate a fully
    grounded candidate — this was the 65.6% coref-floor on real turns."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(text="Alice works at Acme now and that office is downtown.")
    to_type, direct = r.route([cand])
    assert cand in direct
    assert r.last_stats["by_reason"].get("coreference", 0) == 0


def test_pronoun_subject_escalates_as_coreference():
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(subject="She", text="She works at Acme.")
    to_type, _ = r.route([cand])
    assert cand in to_type
    assert r.last_stats["by_reason"]["coreference"] == 1


def test_pronoun_object_escalates_as_coreference():
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(obj="it", text="Alice really likes it.", predicate="likes")
    to_type, _ = r.route([cand])
    assert cand in to_type
    assert r.last_stats["by_reason"]["coreference"] == 1


def test_ungrounded_subject_with_ellipsis_lead_escalates():
    """Zero-pronoun clause: no subject span, not the self-entity, leads with a
    conjunction/bare verb — still coref (subject carried from prior turn)."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = Candidate(
        subject_name="Berlin", predicate="lives_in", object_literal="Berlin",
        fact_text="and moved to Berlin last spring", valid_at=0,
        confidence=0.9, source="test",
        subject_span=None, object_span=(13, 19), needs_typing=False,
    )
    to_type, _ = r.route([cand])
    assert cand in to_type
    assert r.last_stats["by_reason"]["coreference"] == 1


def test_first_person_self_entity_not_coref():
    """'I moved to Berlin' → subject resolved to the self entity → grounded."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = Candidate(
        subject_name="user", predicate="lives_in", object_literal="Berlin",
        fact_text="I moved to Berlin last week.", valid_at=0,
        confidence=0.9, source="test",
        subject_span=None, object_span=(11, 17), needs_typing=False,
    )
    to_type, direct = r.route([cand])
    assert cand in direct


# ── Task 6 Step 0: subject-only prior_entity (scope amendment, user-approved) ──
# `prior_entity` measured at 57% of real candidates (2026-07 sweep), confidence-
# independent, because the OLD contract escalated on either endpoint. Re-mentioning
# a known entity as the OBJECT is normal discourse; only a non-self entity as the
# candidate's SUBJECT is a genuine cross-turn edge. Object endpoint dropped from the
# check; self-exemption and `introduced_here` logic unchanged.


def test_object_remention_of_known_entity_routes_direct():
    """Re-mentioning a known entity as the OBJECT is normal discourse, not a
    cross-turn edge — measured at 57% of real candidates (2026-07 sweep)."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(subject="user", obj="Acme", text="I visited Acme again today.",
                          predicate="works_at", subject_span=None)
    to_type, direct = r.route([cand], known_entities={"Acme"})
    assert cand in direct
    assert r.last_stats["by_reason"].get("prior_entity", 0) == 0


def test_prior_subject_still_escalates():
    """A non-self subject seen in a PRIOR turn is a genuine cross-turn edge."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(subject="Acme", obj="Berlin", predicate="located_in",
                          text="Acme is located in Berlin.")
    to_type, _ = r.route([cand], known_entities={"Acme"})
    assert cand in to_type
    assert r.last_stats["by_reason"]["prior_entity"] == 1
