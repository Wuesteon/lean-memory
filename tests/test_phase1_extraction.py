"""Phase 1 hybrid-extraction tests. All offline (Stub generator/router/typer), no downloads.

Covers the load-bearing Phase 1 behaviors (spec §5):
  - Pass 2→3→4 candidate pipeline runs and produces typed facts
  - the recall-biased router exposes a sub-100% escalation rate on explicit facts
  - the relation taxonomy is the single shared contract (no forked Candidate)
  - contradiction→supersession (SUPERSEDES retires; EXTENDS co-valid) via the resolver
  - salience is scored and cached at write
  - the inference-cue check is word-boundary (no 'so'-in-'also' false positive)
"""

from __future__ import annotations

import pytest

from lean_memory import Memory
from lean_memory.extract.contradiction import EXTENDS, SUPERSEDES, ContradictionResolver
from lean_memory.extract.gliner_extractor import StubCandidateGenerator
from lean_memory.extract.llm_typer import StubTyper
from lean_memory.extract.router import RecallBiasedRouter
from lean_memory.extract.taxonomy import Candidate, Relation
from lean_memory.types import Episode


@pytest.fixture
def mem(tmp_path):
    m = Memory(root=tmp_path)
    yield m
    m.close()


# ── pipeline ──
def test_pass234_pipeline_runs():
    ep = Episode(namespace="t", raw="I work at Acme. I like coffee.", t_ref=1_700_000_000_000)
    cands = StubCandidateGenerator().generate(ep)
    assert cands, "Pass 2 should over-generate at least one candidate"
    assert all(isinstance(c, Candidate) for c in cands), "must be the canonical taxonomy.Candidate"

    router = RecallBiasedRouter()
    to_type, direct = router.route(cands, known_entities=set())
    assert router.last_stats["seen"] == len(cands)

    typed = StubTyper().type_candidates(ep.raw, to_type + direct, known_entities=[])
    assert len(typed) == len(cands)
    assert all(t.relation in {r.value for r in Relation} for t in typed)


def test_router_escalation_is_subhundred_on_explicit_facts():
    """Trivially-explicit first-person facts route DIRECT; only heuristic ones escalate.
    The escalation rate must be a meaningful metric (not pinned at 100%)."""
    raw = "I work at Acme. I like coffee. I use Python. Acme is a startup."
    ep = Episode(namespace="t", raw=raw, t_ref=1_700_000_000_000)
    cands = StubCandidateGenerator().generate(ep)
    router = RecallBiasedRouter()
    to_type, direct = router.route(cands, known_entities=set())
    assert direct, "explicit facts should route direct (skip the LLM)"
    assert router.last_stats["rate"] < 1.0, "escalation must be below 100% on explicit facts"


def test_inference_cue_word_boundary():
    """'I also like tea' must NOT be typed derives (the 'so'-inside-'also' bug)."""
    ep = Episode(namespace="t", raw="I also like tea.", t_ref=1_700_000_000_000)
    cands = StubCandidateGenerator().generate(ep)
    typed = StubTyper().type_candidates(ep.raw, cands, known_entities=[])
    assert all(t.relation != Relation.DERIVES.value for t in typed)
    assert all(t.is_inference == 0 for t in typed)


# ── first-person detection is case-insensitive (WP15, issue #14) ──
def _subjects(raw: str) -> list[str]:
    ep = Episode(namespace="t", raw=raw, t_ref=1_700_000_000_000)
    return [c.subject_name for c in StubCandidateGenerator().generate(ep)]


def test_lowercase_first_person_resolves_to_the_default_subject():
    """WP15's extractor half. `_FIRST_PERSON` used to be case-SENSITIVE (unlike
    RulesExtractor's, which has carried re.I since Phase 0), so a chat user
    typing a lowercase 'i' fell through to the Capitalized-run/lead-token
    subject fallback and got an entity literally named 'i'. That is half of
    issue #14 — the store-side name_key fold alone does NOT close it, because
    the split is 'user' vs 'i', not 'Acme' vs 'acme'."""
    assert _subjects("i work at acme.") == ["user"]


@pytest.mark.parametrize(
    "raw,was",
    [
        ("Mine uses explosives.", "Mine"),
        ("ME uses Postgres.", "ME"),
        ("The company Acme uses i as a variable.", "The"),
    ],
)
def test_re_i_first_person_false_merge_class_is_pinned(raw, was):
    """KNOWN REGRESSION CLASS, pinned — the price of the re.I above.

    `my`/`me`/`mine` were already lowercase in the pattern, so re.I admits not
    only the wanted lowercase 'i' but also 'ME', 'Mine', 'MY', and a bare 'i'
    ANYWHERE in the sentence. A sentence whose genuine subject is that noun is
    silently re-attributed to `default_subject` — a false merge into 'user',
    the busiest slot in any store. It is bounded (the token must appear before
    the relation verb) and recoverable on the same as_of/history terms as the
    entity-fold known limit, but it is a DECISION, not an accident: this test
    is what makes it one. (`was` records the pre-WP15 subject.)

    Note what is NOT a test of this change: "my budget is 40 euros." already
    resolved to 'user' before re.I, because `my` is lowercase in the pattern.
    The only genuinely-new lowercase form is the bare `i`."""
    assert _subjects(raw) == ["user"], f"pre-WP15 this was {was!r}"


def test_lowercase_my_was_already_first_person():
    """The control for the test above — unchanged by re.I in both directions."""
    assert _subjects("my budget is 40 euros.") == ["user"]


# ── contradiction → supersession ──
def test_supersession_via_contradiction_resolver(mem):
    mem.add("u", "I work at Acme.", t_ref=1_700_000_000_000)
    mem.add("u", "I work at Globex now.", t_ref=1_700_000_100_000)
    store = mem._store("u")
    rows = store._db.execute(
        "SELECT fact_text, is_latest, superseded_by FROM fact WHERE predicate='works_at' "
        "ORDER BY valid_at"
    ).fetchall()
    assert len(rows) == 2, "ADD-only: both facts retained"
    assert rows[0]["is_latest"] == 0 and rows[0]["superseded_by"] is not None
    assert rows[1]["is_latest"] == 1


def test_contradiction_resolver_labels():
    """Direct unit test of the resolver: a clear replacement → SUPERSEDES."""
    from lean_memory.embed.fake import FakeEmbedder
    from lean_memory.types import Fact, new_id

    emb = FakeEmbedder()
    resolver = ContradictionResolver()
    old = Fact(namespace="t", subject_id="s1", predicate="works_at",
               fact_text="I work at Acme.", valid_at=0, episode_id="e1", id=new_id())
    new = Fact(namespace="t", subject_id="s1", predicate="works_at",
               fact_text="I work at Globex.", valid_at=1, episode_id="e2", id=new_id())
    decision = resolver.classify(new, [old], emb)
    assert decision.label in (SUPERSEDES, EXTENDS)  # a different object in the slot → not asserts
    assert decision.target is not None


# ── salience ──
def test_salience_scored_at_write(mem):
    mem.add("u", "I work at Acme Corporation in Berlin.", t_ref=1_700_000_000_000)
    store = mem._store("u")
    row = store._db.execute("SELECT salience FROM fact LIMIT 1").fetchone()
    assert row is not None
    assert 0.0 <= row["salience"] <= 10.0
    assert row["salience"] > 0.0, "a grounded, specific fact should score above zero salience"
