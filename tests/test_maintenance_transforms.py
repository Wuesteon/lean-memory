"""Task 4 — MaintenanceConfig, scoring, summarizer, and the four transforms.

All offline (FakeEmbedder, deterministic stub summarizer) against a real
SqliteStore. Pins the design-spec §3.6/§4.1–§4.4 behavior:
  - config hash stability
  - value() recency anchor incl. the backfill case
  - extractive stub summarizer pinned output
  - value-preserving normalization (case/whitespace/NFC) + NEVER-merge distinct values
  - DEDUP-EXACT survivor rule + tiebreak + usage-stats merge
  - multivalued slots never auto-merged; only DEDUP-NEAR proposes them
  - EVICT guards + strict auto-band
  - intra-run ordering: a staged-proposal target is excluded from the autos
  - proposal budget truncation is reported, not silent
  - all three propose transforms make ZERO spine writes
"""

from __future__ import annotations

import unicodedata

import pytest

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.maintain import (
    ExtractiveStubSummarizer,
    MaintenanceConfig,
    MS_PER_DAY,
    dedup_exact,
    dedup_near,
    evict_auto,
    evict_propose,
    normalize_text,
    run_transforms,
    summarize,
    value,
)
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact

NOW = 2_000_000_000_000  # fixed wall-clock for reproducibility (epoch ms)


# ── fixtures / helpers ────────────────────────────────────────────────────────
@pytest.fixture
def store(tmp_path):
    emb = FakeEmbedder()
    s = SqliteStore(tmp_path / "ns.db", dim=emb.dim, coarse_dim=emb.coarse_dim)
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    s.add_episode(ep)
    subj = s.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    s._emb = emb
    s._ep = ep
    s._subj = subj
    yield s
    s.close()


def _run(store):
    """Claim a maintenance run and return its id (proposals need a run_id)."""
    return store.create_run("ns", "cli", NOW, MaintenanceConfig().config_hash())


def _add(
    store,
    text,
    *,
    predicate="works_at",
    valid_at,
    salience=5.0,
    access_count=0,
    last_access=None,
    is_latest=1,
    record_kind="fact",
    subject_id=None,
    embed_text=None,
):
    """Insert a fact. `embed_text` overrides what gets embedded (to force cosine bands)."""
    f = Fact(
        namespace="ns",
        subject_id=subject_id or store._subj.id,
        predicate=predicate,
        fact_text=text,
        valid_at=valid_at,
        episode_id=store._ep.id,
        salience=salience,
        access_count=access_count,
        last_access=last_access,
        is_latest=is_latest,
        record_kind=record_kind,
    )
    full, coarse = store._emb.embed_with_coarse(embed_text if embed_text is not None else text)
    store.add_fact(f, full, coarse)
    return f


def _fact_table_dump(store):
    """Full ordered dump of the fact table — the ZERO-spine-delta oracle."""
    return store._db.execute(
        "SELECT id, is_latest, valid_at, valid_to, superseded_by, tier, "
        "access_count, last_access, record_kind FROM fact ORDER BY id"
    ).fetchall()


def _dump_tuples(store):
    return [tuple(r) for r in _fact_table_dump(store)]


# ── config ────────────────────────────────────────────────────────────────────
def test_config_defaults_match_spec():
    c = MaintenanceConfig()
    assert c.tau_near == 0.95
    assert c.age_floor_days == 90
    assert c.min_cluster == 5
    assert c.proposal_expiry_days == 30
    assert c.proposal_budget_per_run == 50
    assert c.auto_evict_salience == 2.0
    assert c.auto_evict_age_days == 180
    assert c.min_new_facts == 200
    assert c.min_new_salience == 300.0
    assert c.max_days_between_runs == 7


def test_config_hash_is_stable_and_sensitive():
    a = MaintenanceConfig()
    b = MaintenanceConfig()
    assert a.config_hash() == b.config_hash(), "same fields → same hash"
    assert len(a.config_hash()) == 64, "sha256 hex digest"
    c = MaintenanceConfig(tau_near=0.9)
    assert c.config_hash() != a.config_hash(), "different field → different hash"


def test_config_is_frozen():
    c = MaintenanceConfig()
    with pytest.raises(Exception):
        c.tau_near = 0.5  # frozen dataclass


# ── score.value ───────────────────────────────────────────────────────────────
def test_value_in_unit_range_and_weighted():
    f = Fact(
        namespace="ns", subject_id="s", predicate="p", fact_text="x",
        valid_at=NOW, episode_id="e", salience=10.0, access_count=10, last_access=NOW,
    )
    v = value(f, NOW)
    assert 0.0 <= v <= 1.0
    # salience 10 → 0.5, recency (age 0) → 0.3, access log1p(10)/log1p(10)=1 → 0.2
    assert v == pytest.approx(1.0, abs=1e-9)


def test_value_backfill_anchor_scores_stale(store):
    """A BACKFILLED fact — old valid_at, NO last_access — must score stale even though
    it was just ingested (§4.4: anchor is (last_access or valid_at), never ingest time)."""
    old_valid = NOW - 400 * MS_PER_DAY  # >1yr in the world-time past
    backfilled = Fact(
        namespace="ns", subject_id="s", predicate="p", fact_text="old news",
        valid_at=old_valid, episode_id="e", salience=0.0, access_count=0,
        last_access=None,
    )
    fresh = Fact(
        namespace="ns", subject_id="s", predicate="p", fact_text="new news",
        valid_at=NOW, episode_id="e", salience=0.0, access_count=0, last_access=None,
    )
    # Both have salience 0 & 0 accesses → only the recency term differs.
    assert value(backfilled, NOW) < value(fresh, NOW)
    # The stale one's recency term is ~0 (400 days of decay), so value ≈ 0.
    assert value(backfilled, NOW) < 0.05


def test_value_last_access_overrides_valid_at():
    """last_access present → it is the anchor, NOT valid_at."""
    old_valid = NOW - 400 * MS_PER_DAY
    recently_read = Fact(
        namespace="ns", subject_id="s", predicate="p", fact_text="x",
        valid_at=old_valid, episode_id="e", salience=0.0, access_count=0,
        last_access=NOW,  # read just now → fresh despite ancient valid_at
    )
    assert value(recently_read, NOW) > 0.25  # recency term near its 0.3 max


# ── extractive stub summarizer ────────────────────────────────────────────────
def test_stub_summarizer_pinned_output(store):
    f1 = _add(store, "user likes tea", valid_at=1_000, salience=3.0)
    f2 = _add(store, "user likes coffee", valid_at=2_000, salience=8.0)
    f3 = _add(store, "user likes water", valid_at=3_000, salience=5.0)
    out = ExtractiveStubSummarizer().summarize([f1, f2, f3])
    # Ordered by DESC salience, tie-break ASC id: coffee(8) water(5) tea(3).
    assert out == "Summary (extractive): user likes coffee user likes water user likes tea"


def test_stub_summarizer_backend_id():
    assert ExtractiveStubSummarizer().backend_id == "stub"


# ── normalization (value-preserving ONLY) ─────────────────────────────────────
def test_normalize_case_and_whitespace():
    assert normalize_text("User  Likes\tCoffee") == normalize_text("user likes coffee")


def test_normalize_nfc_equivalence():
    # 'é' as NFC single codepoint vs NFD 'e' + combining accent → same normal form.
    nfc = "café"          # café (composed)
    nfd = "café"          # café (decomposed)
    assert unicodedata.normalize("NFC", nfd) == nfc  # sanity on the fixture
    assert normalize_text(nfc) == normalize_text(nfd)


def test_normalize_never_merges_distinct_values():
    """The load-bearing safety property: distinct values NEVER share a normal form."""
    assert normalize_text("salary 100k") != normalize_text("salary 110k")
    assert normalize_text("likes jazz") != normalize_text("likes blues")


# ── DEDUP-EXACT ────────────────────────────────────────────────────────────────
def test_dedup_exact_survivor_argmin_valid_at(store):
    survivor = _add(store, "user works at acme", valid_at=1_000)
    loser = _add(store, "USER  works at ACME", valid_at=2_000)  # same value, diff case/ws
    slot = (store._subj.id, "works_at")
    merges = dedup_exact(store, MaintenanceConfig(), NOW, [slot])
    assert len(merges) == 1
    assert merges[0].survivor_id == survivor.id  # argmin(valid_at)
    assert merges[0].loser_ids == [loser.id]
    # loser retired (verb (c)): is_latest=0, superseded_by=survivor, valid_to UNTOUCHED
    row = store._db.execute(
        "SELECT is_latest, superseded_by, valid_to FROM fact WHERE id=?", (loser.id,)
    ).fetchone()
    assert row["is_latest"] == 0
    assert row["superseded_by"] == survivor.id
    assert row["valid_to"] is None


def test_dedup_exact_tiebreak_min_id(store):
    """Equal valid_at → survivor is min id."""
    a = _add(store, "user works at acme", valid_at=5_000)
    b = _add(store, "user works at acme", valid_at=5_000)
    survivor_id = min(a.id, b.id)
    slot = (store._subj.id, "works_at")
    merges = dedup_exact(store, MaintenanceConfig(), NOW, [slot])
    assert merges[0].survivor_id == survivor_id


def test_dedup_exact_merges_usage_stats(store):
    """access_count summed over cluster; last_access = max coalesce(last_access, valid_at)."""
    survivor = _add(store, "user works at acme", valid_at=1_000, access_count=2, last_access=1_500)
    loser = _add(store, "user works at acme", valid_at=9_000, access_count=3, last_access=None)
    slot = (store._subj.id, "works_at")
    dedup_exact(store, MaintenanceConfig(), NOW, [slot])
    row = store._db.execute(
        "SELECT access_count, last_access FROM fact WHERE id=?", (survivor.id,)
    ).fetchone()
    assert row["access_count"] == 5  # 2 + 3
    # max(coalesce(1500, 1000), coalesce(None→9000, 9000)) = 9000
    assert row["last_access"] == 9_000


def test_dedup_exact_excludes_summaries(store):
    """record_kind='summary' rows are never a dedup target."""
    _add(store, "user works at acme", valid_at=1_000, record_kind="summary", predicate="summary")
    _add(store, "user works at acme", valid_at=2_000, record_kind="summary", predicate="summary")
    slot = (store._subj.id, "summary")
    merges = dedup_exact(store, MaintenanceConfig(), NOW, [slot])
    assert merges == []


def test_dedup_exact_distinct_values_never_merged(store):
    """Different values in the same slot are NEVER merged (normalization is safe)."""
    _add(store, "salary 100k", predicate="salary", valid_at=1_000)
    _add(store, "salary 110k", predicate="salary", valid_at=2_000)
    slot = (store._subj.id, "salary")
    merges = dedup_exact(store, MaintenanceConfig(), NOW, [slot])
    assert merges == []


def test_dedup_exact_multivalued_distinct_never_merged(store):
    """'likes jazz' / 'likes blues' — distinct multivalued values — never auto-merged."""
    _add(store, "likes jazz", predicate="likes", valid_at=1_000)
    _add(store, "likes blues", predicate="likes", valid_at=2_000)
    slot = (store._subj.id, "likes")
    merges = dedup_exact(store, MaintenanceConfig(), NOW, [slot])
    assert merges == []


# ── DEDUP-NEAR (propose) ───────────────────────────────────────────────────────
def test_dedup_near_proposes_high_cosine_pair(store):
    """Two non-identical texts with cosine >= tau_near → one proposal, no spine write."""
    # Force cosine >= 0.95 by embedding both near-identically but keeping text distinct.
    f1 = _add(store, "likes jazz music", predicate="likes", valid_at=1_000, embed_text="MUSICVEC")
    f2 = _add(store, "likes jazz tunes", predicate="likes", valid_at=2_000, embed_text="MUSICVEC")
    # identical embed_text → cosine 1.0 (>= tau); texts differ → not exact dup.
    run_id = _run(store)
    before = _dump_tuples(store)
    staged, dropped = dedup_near(store, MaintenanceConfig(), NOW, [(store._subj.id, "likes")], run_id=run_id)
    assert dropped == 0
    assert len(staged) == 1
    assert set(staged[0].fact_ids) == {f1.id, f2.id}
    # ZERO spine writes.
    assert _dump_tuples(store) == before
    prop = store.get_proposal(staged[0].proposal_id)
    assert prop["kind"] == "dedup_near"
    assert prop["evidence_backend"] == "stored"
    import json
    payload = json.loads(prop["payload_json"])
    assert payload["multivalued"] is True  # 'likes' is a multivalued predicate
    assert payload["cosine"] >= 0.95
    assert payload["proposed_survivor"] == f1.id  # argmin valid_at


def test_dedup_near_ignores_low_cosine(store):
    _add(store, "likes jazz", predicate="likes", valid_at=1_000, embed_text="AAA")
    _add(store, "likes opera", predicate="likes", valid_at=2_000, embed_text="ZZZ")
    run_id = _run(store)
    staged, _ = dedup_near(store, MaintenanceConfig(), NOW, [(store._subj.id, "likes")], run_id=run_id)
    assert staged == []  # FakeEmbedder gives near-orthogonal vectors for distinct text


def test_dedup_near_skips_exact_duplicates(store):
    """Textually identical (post-normalize) pairs belong to DEDUP-EXACT, not near."""
    _add(store, "likes jazz", predicate="likes", valid_at=1_000, embed_text="SAME")
    _add(store, "LIKES JAZZ", predicate="likes", valid_at=2_000, embed_text="SAME")
    run_id = _run(store)
    staged, _ = dedup_near(store, MaintenanceConfig(), NOW, [(store._subj.id, "likes")], run_id=run_id)
    assert staged == []


# ── SUMMARIZE (propose) ────────────────────────────────────────────────────────
def test_summarize_proposes_old_cluster(store):
    """>= min_cluster old-enough facts in a slot → one summarize proposal, no spine write."""
    old = NOW - 200 * MS_PER_DAY  # older than age_floor (90d)
    for i in range(5):
        _add(store, f"note number {i}", predicate="notes", valid_at=old + i)
    run_id = _run(store)
    before = _dump_tuples(store)
    staged, dropped = summarize(store, MaintenanceConfig(), NOW, ExtractiveStubSummarizer(), run_id=run_id)
    assert dropped == 0
    assert len(staged) == 1
    assert _dump_tuples(store) == before  # ZERO spine writes
    prop = store.get_proposal(staged[0].proposal_id)
    assert prop["kind"] == "summarize"
    assert prop["evidence_backend"] == "stub"


def test_summarize_exhausted_budget_never_invokes_summarizer(store):
    """A full budget must SHORT-CIRCUIT before the summarizer runs (§4.3): the summary
    text would just be discarded, and once Ollama is the [llm] summarizer that is a
    wasted generation. A qualifying cluster + budget 0 → dropped, spy called ZERO times."""
    old = NOW - 200 * MS_PER_DAY  # older than age_floor (90d)
    for i in range(5):
        _add(store, f"note number {i}", predicate="notes", valid_at=old + i)
    run_id = _run(store)

    class _SpySummarizer:
        backend_id = "spy"

        def __init__(self):
            self.calls = 0

        def summarize(self, sources):
            self.calls += 1
            return "should never run"

    spy = _SpySummarizer()
    staged, dropped = summarize(store, MaintenanceConfig(), NOW, spy, run_id=run_id, budget=0)
    assert staged == []
    assert dropped == 1  # the qualifying cluster was dropped, not silently skipped
    assert spy.calls == 0  # budget checked BEFORE the summarizer was ever invoked


def test_summarize_skips_small_or_young_clusters(store):
    old = NOW - 200 * MS_PER_DAY
    # too small (4 < min_cluster 5)
    for i in range(4):
        _add(store, f"a{i}", predicate="small", valid_at=old + i)
    # big but too young (< age_floor)
    for i in range(6):
        _add(store, f"b{i}", predicate="young", valid_at=NOW - 1_000 + i)
    run_id = _run(store)
    staged, _ = summarize(store, MaintenanceConfig(), NOW, ExtractiveStubSummarizer(), run_id=run_id)
    assert staged == []


# ── EVICT ──────────────────────────────────────────────────────────────────────
def test_evict_auto_band_demotes(store):
    """salience<2 AND access_count==0 AND age>180d → demoted to cold without review."""
    old = NOW - 200 * MS_PER_DAY
    f = _add(store, "trivial old note", valid_at=old, salience=1.0, access_count=0)
    demoted = evict_auto(store, MaintenanceConfig(), NOW)
    assert demoted == [f.id]
    row = store._db.execute("SELECT tier FROM fact WHERE id=?", (f.id,)).fetchone()
    assert row["tier"] == "cold"
    vrow = store._db.execute("SELECT tier FROM fact_vec WHERE fact_id=?", (f.id,)).fetchone()
    assert vrow["tier"] == "cold"  # two-surface sync


def test_evict_guard_high_salience(store):
    """salience >= 6 is never demoted, even if old + unaccessed."""
    old = NOW - 400 * MS_PER_DAY
    _add(store, "important old fact", valid_at=old, salience=9.0, access_count=0)
    run_id = _run(store)
    assert evict_auto(store, MaintenanceConfig(), NOW) == []
    staged, _ = evict_propose(store, MaintenanceConfig(), NOW, run_id=run_id)
    assert staged == []


def test_evict_guard_young(store):
    """age < age_floor_days is never demoted/proposed regardless of salience."""
    _add(store, "recent low note", valid_at=NOW - 1_000, salience=0.0, access_count=0)
    run_id = _run(store)
    assert evict_auto(store, MaintenanceConfig(), NOW) == []
    staged, _ = evict_propose(store, MaintenanceConfig(), NOW, run_id=run_id)
    assert staged == []


def test_evict_guard_summary(store):
    """record_kind='summary' is never demoted/proposed."""
    old = NOW - 400 * MS_PER_DAY
    _add(store, "old summary", predicate="summary", valid_at=old, salience=0.0,
         access_count=0, record_kind="summary")
    run_id = _run(store)
    assert evict_auto(store, MaintenanceConfig(), NOW) == []
    staged, _ = evict_propose(store, MaintenanceConfig(), NOW, run_id=run_id)
    assert staged == []


def test_evict_access_count_zero_not_sufficient_alone(store):
    """access_count==0 alone never demotes: a salient, recent-ish, unaccessed fact
    with salience above the auto band and value above threshold is left alone."""
    old = NOW - 100 * MS_PER_DAY  # past age_floor(90) but before auto band(180)
    f = _add(store, "unaccessed but salient", valid_at=old, salience=5.0, access_count=0)
    run_id = _run(store)
    assert evict_auto(store, MaintenanceConfig(), NOW) == []  # not in auto band (age<180, sal>2)
    staged, _ = evict_propose(store, MaintenanceConfig(), NOW, run_id=run_id)
    # value: 0.5*(5/10)=0.25 salience alone >= evict_threshold(0.15) → no proposal
    assert value(f, NOW) >= MaintenanceConfig().evict_threshold
    assert staged == []


def test_evict_proposes_below_threshold_out_of_band(store):
    """Low value, guarded, but NOT in the strict auto-band → proposal (no spine write)."""
    # age between age_floor(90) and auto band(180): 120d. salience 0.5 → value ~0.25*... low.
    old = NOW - 120 * MS_PER_DAY
    f = _add(store, "meh note", valid_at=old, salience=0.5, access_count=0)
    assert not (NOW - f.valid_at > 180 * MS_PER_DAY)  # confirm out of auto band
    assert value(f, NOW) < MaintenanceConfig().evict_threshold
    run_id = _run(store)
    before = _dump_tuples(store)
    staged, dropped = evict_propose(store, MaintenanceConfig(), NOW, run_id=run_id)
    assert len(staged) == 1 and staged[0].fact_ids == [f.id]
    assert _dump_tuples(store) == before  # ZERO spine writes
    assert store.get_proposal(staged[0].proposal_id)["kind"] == "evict"


# ── intra-run ordering + budget (run_transforms) ───────────────────────────────
def test_run_transforms_excludes_staged_from_autos(store):
    """A fact referenced by a staged proposal is EXCLUDED from the auto transforms (§4.4).

    Construct a fact that would BOTH be proposed (near-dup) AND auto-band-demotable,
    and assert staging wins: it is NOT demoted."""
    old = NOW - 200 * MS_PER_DAY  # auto-band eligible (age>180, sal<2, access 0)
    # Near-dup pair on a 'likes' slot, both low-salience/old/unaccessed.
    a = _add(store, "likes vintage jazz", predicate="likes", valid_at=old, salience=1.0,
             access_count=0, embed_text="NEARDUP")
    b = _add(store, "likes classic jazz", predicate="likes", valid_at=old + 1, salience=1.0,
             access_count=0, embed_text="NEARDUP")
    run_id = _run(store)
    slots = [(store._subj.id, "likes")]
    report = run_transforms(store, MaintenanceConfig(), NOW, run_id=run_id, slots=slots)
    # Both facts were staged in the near-dup proposal → excluded from auto-evict.
    assert len(report.proposals) >= 1
    staged_ids = report.staged_fact_ids
    assert a.id in staged_ids and b.id in staged_ids
    assert a.id not in report.demoted_ids and b.id not in report.demoted_ids
    # And they were NOT auto-deduped either (excluded from dedup_exact).
    for f in (a, b):
        row = store._db.execute("SELECT is_latest, tier FROM fact WHERE id=?", (f.id,)).fetchone()
        assert row["is_latest"] == 1  # untouched by autos
        assert row["tier"] == "hot"


def test_run_transforms_budget_truncation_reported(store):
    """When staging exceeds proposal_budget_per_run, the drop is REPORTED, not silent."""
    cfg = MaintenanceConfig(proposal_budget_per_run=2)
    old = NOW - 200 * MS_PER_DAY
    # Build 4 near-dup pairs on 4 distinct slots → 4 candidate proposals, budget 2.
    slots = []
    for p in range(4):
        pred = f"likes{p}"
        _add(store, f"likes thing {p} alpha", predicate=pred, valid_at=old, embed_text=f"DUP{p}")
        _add(store, f"likes thing {p} beta", predicate=pred, valid_at=old + 1, embed_text=f"DUP{p}")
        slots.append((store._subj.id, pred))
    run_id = _run(store)
    report = run_transforms(store, cfg, NOW, run_id=run_id, slots=slots)
    assert len(report.proposals) == 2, "budget caps staged proposals"
    assert report.dropped_proposals == 2, "the 2 dropped are reported, not silent"


def test_run_transforms_all_propose_transforms_zero_spine_delta(store):
    """Staging (all three propose transforms via run_transforms) writes ZERO spine
    changes — the full fact-table dump is unchanged by the propose phase.

    (Autos here have nothing to do: the near-dup pair is excluded from dedup_exact,
    and no fact is in the auto-evict band once staged.)"""
    old = NOW - 200 * MS_PER_DAY
    _add(store, "likes deep jazz", predicate="likes", valid_at=old, salience=1.0, embed_text="Z")
    _add(store, "likes cool jazz", predicate="likes", valid_at=old + 1, salience=1.0, embed_text="Z")
    for i in range(5):
        _add(store, f"old cluster note {i}", predicate="notes", valid_at=old + i, salience=1.0)
    run_id = _run(store)
    before = _dump_tuples(store)
    report = run_transforms(
        store, MaintenanceConfig(), NOW, run_id=run_id,
        slots=[(store._subj.id, "likes"), (store._subj.id, "notes")],
    )
    assert report.proposals, "something was staged"
    # No auto merges/demotions happened here (all candidates were staged/excluded),
    # so the entire fact table is byte-identical to before the run.
    assert _dump_tuples(store) == before
