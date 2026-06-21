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
        cand = _cand("Sam", "lives_in", "Paris", "She lives in Paris.")
        to_type, direct = router.route([cand], known_entities=["Sam"], self_entity="user")
        assert cand in to_type, "Pronoun coreference should still escalate"
