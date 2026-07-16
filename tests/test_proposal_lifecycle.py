"""Proposal lifecycle — decide + apply, the maintenance review path (spec §4.2-§4.4, §5, §10).

Covers, TDD-first:
  - CAS double-decide across two store handles on ONE file (second returns
    already-decided, no re-apply); re-apply retry after commit returns "already
    applied", not an error (§5, §10.7).
  - Lazy timeout expiry: a pending proposal past expires_at is expired by decide().
  - STALE-TARGET expiry: a dedup target superseded between stage and approve flips the
    proposal to expired/stale_target while the SPINE stays byte-identical (full fact-
    table hash) (§5, §10.7).
  - REJECT leaves the spine byte-identical (§10.7).
  - EDITED-approve records human provenance (edited_text stored, status='edited') and
    re-scores the summary with source='user', outranking a machine-scored sibling
    (salience delta) (§4.3).
  - SUMMARIZE apply end-to-end: maintenance episode + summary fact (valid_at=t_a, tier
    hot, record_kind summary) + derivation rows + sources cold + old-summary
    supersession on a second approve (§4.3).
  - EVICT apply + PROMOTE verb round-trip (§4.4).
  - Dim-mismatch embedder refuses to apply with a clear error (§4.3 apply-ownership).
  - THE HEADLINE: apply-path as-of grid re-run — approve one summarize + one dedup_near
    + one evict via a real runner pass; the store predicate is bit-identical for all
    T < t_a and shows only the intended deltas at T >= t_a (§10.1).

Offline on FakeEmbedder; LM_FORCE_STUBS honored by the default summarizer.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.maintain import lifecycle
from lean_memory.maintain.config import MS_PER_DAY, MaintenanceConfig
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact

T_A = 2_000_000_000_000  # apply time (epoch ms)
DAY = MS_PER_DAY


# ── fixtures / helpers ─────────────────────────────────────────────────────────
@pytest.fixture
def emb():
    return FakeEmbedder()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "lifecycle.db"


def _open(db_path, emb, *, busy_timeout_ms=5000):
    s = SqliteStore(db_path, dim=emb.dim, coarse_dim=emb.coarse_dim,
                    busy_timeout_ms=busy_timeout_ms)
    return s


def _seed(store, emb):
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    store.add_episode(ep)
    store._emb = emb
    store._ep = ep
    store._subj = store.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    return store


def _add(store, text, *, predicate="likes", valid_at=T_A - 300 * DAY, valid_to=None,
         superseded_by=None, is_latest=1, salience=5.0, access_count=0,
         last_access=None, tier="hot", record_kind="fact"):
    f = Fact(
        namespace="ns", subject_id=store._subj.id, predicate=predicate,
        fact_text=text, valid_at=valid_at, valid_to=valid_to,
        superseded_by=superseded_by, is_latest=is_latest, episode_id=store._ep.id,
        salience=salience, access_count=access_count, last_access=last_access,
        tier=tier, record_kind=record_kind,
    )
    full, coarse = store._emb.embed_with_coarse(text)
    store.add_fact(f, full, coarse)
    return f


def _run_id(store):
    return store.create_run("ns", "cli", T_A, "cfg")


def _fact_hash(store):
    """A stable hash of the ENTIRE fact table — the §10.7 spine-invariance probe."""
    rows = store._db.execute(
        "SELECT id, is_latest, valid_at, valid_to, superseded_by, invalidated_by, "
        "tier, access_count, last_access, record_kind, salience "
        "FROM fact ORDER BY id"
    ).fetchall()
    blob = json.dumps([tuple(r) for r in rows], sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def _visible_at(store, T):
    rows = store._db.execute(
        "SELECT id FROM fact WHERE valid_at <= ? AND (valid_to IS NULL OR valid_to > ?)",
        (T, T),
    ).fetchall()
    return frozenset(r["id"] for r in rows)


def _stage_dedup_near(store, run_id, a, b, survivor):
    payload = {
        "slot": {"subject_id": a.subject_id, "predicate": a.predicate},
        "fact_ids": [a.id, b.id],
        "fact_texts": {a.id: a.fact_text, b.id: b.fact_text},
        "cosine": 0.99,
        "multivalued": True,
        "proposed_survivor": survivor.id,
        "evidence_backend": "stored",
    }
    return store.stage_proposal(
        run_id=run_id, namespace="ns", kind="dedup_near",
        payload_json=json.dumps(payload, sort_keys=True),
        created_at=T_A, expires_at=T_A + 30 * DAY, evidence_backend="stored",
    )


def _stage_summarize(store, run_id, sources, summary_text, subject_id=None):
    payload = {
        "subject_id": subject_id or sources[0].subject_id,
        "source_fact_ids": [f.id for f in sources],
        "source_fact_texts": {f.id: f.fact_text for f in sources},
        "summary_text": summary_text,
        "evidence_backend": "stub",
    }
    return store.stage_proposal(
        run_id=run_id, namespace="ns", kind="summarize",
        payload_json=json.dumps(payload, sort_keys=True),
        created_at=T_A, expires_at=T_A + 30 * DAY, evidence_backend="stub",
    )


def _pick(proposals, predicate):
    """The first proposal matching `predicate` — raises if none (keeps the grid test
    honest: a corpus change that stops producing the target proposal fails loudly)."""
    for p in proposals:
        if predicate(p):
            return p
    raise AssertionError("no proposal matched the picker predicate")


def _stage_evict(store, run_id, fact):
    payload = {
        "fact_id": fact.id, "fact_text": fact.fact_text, "value": 0.1,
        "salience": fact.salience, "access_count": fact.access_count,
        "evidence_backend": "score",
    }
    return store.stage_proposal(
        run_id=run_id, namespace="ns", kind="evict",
        payload_json=json.dumps(payload, sort_keys=True),
        created_at=T_A, expires_at=T_A + 30 * DAY, evidence_backend="score",
    )


# ── CAS double-decide across two handles ───────────────────────────────────────
def test_cas_double_decide_across_two_handles(db_path, emb):
    """Two store handles on the SAME file race to approve one evict proposal. Exactly
    one wins the CAS; the second returns already-decided and does NOT re-apply (§5)."""
    s1 = _seed(_open(db_path, emb), emb)
    fact = _add(s1, "trivial ancient note", predicate="notes", salience=1.0)
    run_id = _run_id(s1)
    pid = _stage_evict(s1, run_id, fact)
    s1.close()

    a = _open(db_path, emb)
    b = _open(db_path, emb)
    r1 = lifecycle.decide(a, emb, pid, "approve", now=T_A, decided_by="console")
    r2 = lifecycle.decide(b, emb, pid, "approve", now=T_A, decided_by="mcp")

    assert r1["outcome"] == "applied"
    assert r2["outcome"] == "already_applied"
    # Exactly ONE apply happened: the fact is cold, and applied_at is set once.
    assert b.get_fact(fact.id).tier == "cold"
    row = b.get_proposal(pid)
    assert row["status"] == "approved"
    assert row["applied_at"] == T_A
    a.close()
    b.close()


def test_reapply_retry_after_commit_returns_already_applied(db_path, emb):
    """A retry of the SAME approve after it committed returns 'already applied', never
    an error and never a second apply (§5)."""
    s = _seed(_open(db_path, emb), emb)
    fact = _add(s, "trivial ancient note", predicate="notes", salience=1.0)
    run_id = _run_id(s)
    pid = _stage_evict(s, run_id, fact)

    first = lifecycle.decide(s, emb, pid, "approve", now=T_A, decided_by="console")
    assert first["outcome"] == "applied"

    # Retry on the same handle.
    second = lifecycle.decide(s, emb, pid, "approve", now=T_A + 1, decided_by="console")
    assert second["outcome"] == "already_applied"
    assert second["applied_at"] == T_A  # unchanged — no re-apply
    s.close()


# ── lazy timeout expiry ────────────────────────────────────────────────────────
def test_timeout_expiry_on_decide(db_path, emb):
    """decide() on a pending proposal past expires_at expires it (timeout) instead of
    deciding it — silence is never consent (§5)."""
    s = _seed(_open(db_path, emb), emb)
    fact = _add(s, "trivial ancient note", predicate="notes", salience=1.0)
    run_id = _run_id(s)
    pid = _stage_evict(s, run_id, fact)
    # expires_at = T_A + 30d; decide at a time PAST that.
    now = T_A + 31 * DAY
    r = lifecycle.decide(s, emb, pid, "approve", now=now, decided_by="console")
    assert r["outcome"] == "expired"
    assert r["expiry_reason"] == "timeout"
    row = s.get_proposal(pid)
    assert row["status"] == "expired" and row["expiry_reason"] == "timeout"
    assert row["applied_at"] is None
    assert s.get_fact(fact.id).tier == "hot"  # never applied
    s.close()


# ── stale-target expiry (dedup) — spine byte-identical ─────────────────────────
def test_stale_target_dedup_expires_and_spine_untouched(db_path, emb):
    """Stage a dedup_near, supersede one target via ordinary ingest, then approve →
    the proposal flips to expired/stale_target and the SPINE is byte-identical to the
    pre-approve state (the approve transaction rolled back; only the expiry committed)
    (§5, §10.7)."""
    s = _seed(_open(db_path, emb), emb)
    a = _add(s, "likes jazz music", valid_at=T_A - 300 * DAY, access_count=1)
    b = _add(s, "enjoys jazz tunes", valid_at=T_A - 250 * DAY, access_count=2)
    run_id = _run_id(s)
    pid = _stage_dedup_near(s, run_id, a, b, survivor=a)

    # Supersede `b` via an ordinary supersession (b is now is_latest=0, closed).
    newer = _add(s, "no longer into jazz", valid_at=T_A - 100 * DAY, is_latest=1)
    s.supersede_fact(b.id, newer.id, valid_to=newer.valid_at)

    hash_before = _fact_hash(s)
    r = lifecycle.decide(s, emb, pid, "approve", now=T_A, decided_by="console")

    assert r["outcome"] == "expired"
    assert r["expiry_reason"] == "stale_target"
    row = s.get_proposal(pid)
    assert row["status"] == "expired" and row["expiry_reason"] == "stale_target"
    assert row["applied_at"] is None
    # THE SPINE IS BYTE-IDENTICAL — the approve verbs never touched it.
    assert _fact_hash(s) == hash_before
    s.close()


# ── reject leaves the spine byte-identical ─────────────────────────────────────
def test_reject_leaves_spine_byte_identical(db_path, emb):
    s = _seed(_open(db_path, emb), emb)
    a = _add(s, "likes jazz music", valid_at=T_A - 300 * DAY)
    b = _add(s, "enjoys jazz tunes", valid_at=T_A - 250 * DAY)
    run_id = _run_id(s)
    pid = _stage_dedup_near(s, run_id, a, b, survivor=a)

    hash_before = _fact_hash(s)
    r = lifecycle.decide(s, emb, pid, "reject", now=T_A, decided_by="console")
    assert r["outcome"] == "rejected"
    assert s.get_proposal(pid)["status"] == "rejected"
    assert _fact_hash(s) == hash_before  # zero spine trace (§3.4/§10.7)
    s.close()


# ── edited-approve: human provenance + source='user' re-score ──────────────────
def test_edited_approve_records_human_provenance_and_outranks_machine(db_path, emb):
    """An edit-approve stores edited_text, marks status='edited', and re-scores the
    summary with source='user' — so it outranks the same text scored as a machine
    (maintenance) summary. Asserts the salience delta directly (§4.3)."""
    s = _seed(_open(db_path, emb), emb)
    sources = [
        _add(s, f"note number {i} about the project", predicate="notes",
             valid_at=T_A - (200 - i) * DAY)
        for i in range(5)
    ]
    run_id = _run_id(s)
    machine_text = "Consolidated project status summary line"

    # (1) Machine approve of a first proposal → summary scored as maintenance source.
    pid_machine = _stage_summarize(s, run_id, sources, machine_text)
    r_machine = lifecycle.decide(s, emb, pid_machine, "approve", now=T_A,
                                 decided_by="console")
    machine_summary = s.get_fact(r_machine["summary_id"])
    machine_salience = machine_summary.salience

    # (2) Edit-approve of a second proposal with the SAME text → summary scored as
    # source='user' (human provenance).
    pid_edit = _stage_summarize(s, run_id, sources, "stub text that gets overridden")
    r_edit = lifecycle.decide(
        s, emb, pid_edit, "edit", now=T_A + DAY, decided_by="console",
        edited_text=machine_text,
    )
    edited_summary = s.get_fact(r_edit["summary_id"])

    # Human provenance recorded on the proposal row.
    row = s.get_proposal(pid_edit)
    assert row["status"] == "edited"
    assert row["edited_text"] == machine_text
    # The edited summary uses the edited text.
    assert edited_summary.fact_text == machine_text
    # source='user' beats source='maintenance' for the SAME text → higher salience.
    assert edited_summary.salience > machine_salience
    s.close()


# ── summarize apply end-to-end ─────────────────────────────────────────────────
def test_summarize_apply_end_to_end(db_path, emb):
    """SUMMARIZE approve: maintenance episode + summary fact (valid_at=t_a, tier hot,
    record_kind summary, is_inference) + derivation rows + sources demoted cold; a
    SECOND approve supersedes the previous summary at t_a (§4.3)."""
    s = _seed(_open(db_path, emb), emb)
    sources = [
        _add(s, f"historical note {i} on topic", predicate="notes",
             valid_at=T_A - (200 - i) * DAY)
        for i in range(5)
    ]
    run_id = _run_id(s)
    pid = _stage_summarize(s, run_id, sources, "Summary line one")

    r = lifecycle.decide(s, emb, pid, "approve", now=T_A, decided_by="console")
    assert r["outcome"] == "applied" and r["kind"] == "summarize"

    summary = s.get_fact(r["summary_id"])
    assert summary.predicate == "summary"
    assert summary.record_kind == "summary"
    assert summary.is_inference == 1
    assert summary.tier == "hot"
    assert summary.valid_at == T_A  # t_a — NEVER backdated
    assert summary.valid_to is None
    assert summary.is_latest == 1

    # Maintenance episode exists with source='maintenance'.
    ep = s._db.execute(
        "SELECT source, raw FROM episode WHERE id=?", (r["episode_id"],)
    ).fetchone()
    assert ep["source"] == "maintenance"
    assert json.loads(ep["raw"])["kind"] == "summarize"

    # Derivation rows: one per source.
    drows = s._db.execute(
        "SELECT source_id FROM fact_derivation WHERE summary_id=?", (summary.id,)
    ).fetchall()
    assert {d["source_id"] for d in drows} == {f.id for f in sources}

    # Sources demoted to cold; still is_latest=1 and as-of visible.
    for f in sources:
        row = s.get_fact(f.id)
        assert row.tier == "cold"
        assert row.is_latest == 1
        assert row.valid_to is None

    # A SECOND summarize approve supersedes the previous summary at t_a.
    t_a2 = T_A + 10 * DAY
    pid2 = _stage_summarize(s, run_id, sources, "Summary line two")
    r2 = lifecycle.decide(s, emb, pid2, "approve", now=t_a2, decided_by="console")
    assert r2["superseded_prev_summary_id"] == summary.id
    old = s.get_fact(summary.id)
    assert old.is_latest == 0
    assert old.valid_to == t_a2
    assert old.superseded_by == r2["summary_id"]
    # No misfire: the old summary is not a SOURCE, so the staleness cascade did not
    # invalidate the new summary — the new one is live.
    assert s.get_fact(r2["summary_id"]).is_latest == 1
    s.close()


def test_summarize_apply_valid_at_not_backdated_grid(db_path, emb):
    """The summary appears in NO past window: it is invisible for every T < t_a and
    visible only at T >= t_a (valid_at=t_a). A guard against a backdated valid_at."""
    s = _seed(_open(db_path, emb), emb)
    sources = [
        _add(s, f"aged note {i}", predicate="notes", valid_at=T_A - (200 - i) * DAY)
        for i in range(5)
    ]
    run_id = _run_id(s)
    pid = _stage_summarize(s, run_id, sources, "Consolidated")
    r = lifecycle.decide(s, emb, pid, "approve", now=T_A, decided_by="console")
    sid = r["summary_id"]

    for T in (T_A - 150 * DAY, T_A - 1, T_A - 0 - 1):
        assert sid not in _visible_at(s, T)
    assert sid in _visible_at(s, T_A)
    assert sid in _visible_at(s, T_A + 50 * DAY)
    s.close()


# ── evict apply + promote round-trip ───────────────────────────────────────────
def test_evict_apply_then_promote_round_trip(db_path, emb):
    """EVICT approve demotes to cold; the PROMOTE verb restores hot; a PROMOTE
    decision on an evict proposal rejects it and promotes (§4.4)."""
    s = _seed(_open(db_path, emb), emb)
    fact = _add(s, "trivial ancient note", predicate="notes", salience=1.0)
    run_id = _run_id(s)
    pid = _stage_evict(s, run_id, fact)

    r = lifecycle.decide(s, emb, pid, "approve", now=T_A, decided_by="console")
    assert r["outcome"] == "applied"
    assert s.get_fact(fact.id).tier == "cold"

    # Direct promote verb restores hot.
    pr = lifecycle.promote_fact(s, fact.id, now=T_A + DAY)
    assert pr["outcome"] == "promoted"
    assert s.get_fact(fact.id).tier == "hot"

    # A PROMOTE *decision* on a fresh evict proposal: rejects it AND promotes.
    fact2 = _add(s, "another ancient note", predicate="notes", salience=1.0, tier="cold")
    pid2 = _stage_evict(s, run_id, fact2)
    dr = lifecycle.decide(s, emb, pid2, "promote", now=T_A + 2 * DAY, decided_by="console")
    assert dr["outcome"] == "promoted"
    assert s.get_proposal(pid2)["status"] == "rejected"
    assert s.get_fact(fact2.id).tier == "hot"
    s.close()


# ── dim-mismatch embedder refuses to apply ─────────────────────────────────────
def test_dim_mismatch_embedder_refuses_summarize_apply(db_path, emb):
    """A summarize apply with an embedder whose dim != the namespace's baked dims is
    refused with a clear error, BEFORE any spine write (§4.3 apply-ownership)."""
    s = _seed(_open(db_path, emb), emb)
    sources = [
        _add(s, f"note {i}", predicate="notes", valid_at=T_A - (200 - i) * DAY)
        for i in range(5)
    ]
    run_id = _run_id(s)
    pid = _stage_summarize(s, run_id, sources, "Consolidated")

    wrong = FakeEmbedder(dim=512, coarse_dim=256)  # dim mismatch (store baked at 768)
    hash_before = _fact_hash(s)
    with pytest.raises(ValueError, match="dim"):
        lifecycle.decide(s, wrong, pid, "approve", now=T_A, decided_by="console")
    # Nothing written; the proposal stays pending.
    assert s.get_proposal(pid)["status"] == "pending"
    assert _fact_hash(s) == hash_before
    s.close()


# ── THE HEADLINE: apply-path as-of grid re-run (spec §10.1) ────────────────────
def _grid_corpus(store):
    """Backfills (id-order != valid_at-order), a functional supersession, a
    multivalued slot with a near-dup pair, plus an evict candidate. Returns notable
    facts. Ages are wide (>90d) so SUMMARIZE/EVICT age gates pass under a tuned cfg."""
    # Functional slot 'works_at': acme (t0) superseded by globex (t1).
    t0 = T_A - 500 * DAY
    t1 = T_A - 300 * DAY
    acme = _add(store, "user works at acme", predicate="works_at", valid_at=t0,
                valid_to=t1, is_latest=0)
    globex = _add(store, "user works at globex", predicate="works_at", valid_at=t1)
    store._db.execute("UPDATE fact SET superseded_by=? WHERE id=?", (globex.id, acme.id))
    store._db.commit()

    # Multivalued slot 'likes': a NEAR-dup pair (distinct text) BACKFILLED so the
    # later-ingested row is older in world-time.
    jazz = _add(store, "likes jazz music a lot", predicate="likes",
                valid_at=T_A - 400 * DAY, access_count=1, last_access=T_A - 350 * DAY)
    jazz2 = _add(store, "enjoys jazz very much indeed", predicate="likes",
                 valid_at=T_A - 450 * DAY, access_count=3, last_access=T_A - 20 * DAY)

    # Enough aged 'notes' facts for a SUMMARIZE cluster (>= min_cluster) on the subject.
    notes = [
        _add(store, f"aged project note number {i}", predicate="notes",
             valid_at=T_A - (300 - i) * DAY, salience=5.0)
        for i in range(5)
    ]

    # A low-value aged fact for an EVICT proposal (not in the strict auto-band because
    # access_count != 0, so it PROPOSES rather than auto-demotes).
    evictable = _add(store, "low value aged remark", predicate="chatter",
                     valid_at=T_A - 300 * DAY, salience=1.0, access_count=2,
                     last_access=T_A - 300 * DAY)

    return {
        "acme": acme, "globex": globex, "jazz": jazz, "jazz2": jazz2,
        "notes": notes, "evictable": evictable,
    }


def test_apply_path_as_of_grid_rerun(db_path, emb):
    """THE HEADLINE (§10.1): stage proposals via a REAL runner pass, approve one of
    each kind (summarize, dedup_near, evict), and assert the store predicate
    (is_latest_only=False) is BIT-IDENTICAL for every T < t_a, with only the intended
    deltas at T >= t_a:
      - the summary fact appears ONLY at T >= t_a (valid_at=t_a, never backdated);
      - the retired near-dup loser stays predicate-visible (valid_to untouched);
      - no inverted intervals.
    """
    from lean_memory.maintain.runner import MaintenanceRunner

    s = _seed(_open(db_path, emb), emb)
    facts = _grid_corpus(s)

    # A tuned config so the FakeEmbedder corpus actually stages each kind: tau_near=0
    # (any distinct same-slot pair proposes), min_cluster=2, small age floor, a high
    # evict_threshold so the low-value fact proposes, and a strict auto-band it evades
    # (access_count != 0). Work thresholds are moot: the first run is always over.
    cfg = MaintenanceConfig(
        tau_near=0.0, min_cluster=2, age_floor_days=1,
        evict_threshold=0.9, auto_evict_salience=0.0, auto_evict_age_days=100000,
        proposal_budget_per_run=100,
    )

    # Snapshot the predicate id-set over a T grid BEFORE any apply.
    grid = [T_A - t * DAY for t in (480, 460, 430, 410, 350, 250, 100, 1)]
    before_sets = {T: _visible_at(s, T) for T in grid}

    # ── Real runner pass stages the proposals (apply=False semantics: staging only) ──
    runner = MaintenanceRunner(s, "ns", config=cfg, now_ms=lambda: T_A)
    report = runner.run()
    assert report.status == "ok"

    pending = s.list_proposals("ns", status="pending", limit=1000)
    by_kind: dict[str, list[dict]] = {}
    for p in pending:
        by_kind.setdefault(p["kind"], []).append(p)
    assert "summarize" in by_kind, "runner staged no summarize proposal"
    assert "dedup_near" in by_kind, "runner staged no dedup_near proposal"
    assert "evict" in by_kind, "runner staged no evict proposal"

    # Pick NON-CONFLICTING proposals of each kind (the propose transforms all run over
    # the same pre-transform snapshot, so a fact can appear in several proposals; we
    # approve a disjoint trio so the three applies don't invalidate each other):
    #   - summarize : the one proposal (its sources are the notes + jazz pair);
    #   - dedup_near: the pair on the 'likes' slot (jazz / jazz2);
    #   - evict     : the 'chatter' fact, referenced by no other approved proposal.
    dn = _pick(by_kind["dedup_near"],
               lambda p: set(json.loads(p["payload_json"])["fact_ids"])
               == {facts["jazz"].id, facts["jazz2"].id})
    ev = _pick(by_kind["evict"],
               lambda p: json.loads(p["payload_json"])["fact_id"] == facts["evictable"].id)
    sm = by_kind["summarize"][0]

    # Approve at apply-time t_a. summarize demotes its notes sources to cold (still
    # is_latest=1), which does NOT invalidate the jazz dedup or the chatter evict.
    t_a = T_A + 1 * DAY
    r_sum = lifecycle.decide(s, emb, sm["id"], "approve", now=t_a, decided_by="console")
    r_dn = lifecycle.decide(s, emb, dn["id"], "approve", now=t_a, decided_by="console")
    r_ev = lifecycle.decide(s, emb, ev["id"], "approve", now=t_a, decided_by="console")
    assert r_sum["outcome"] == "applied"
    assert r_dn["outcome"] == "applied"
    assert r_ev["outcome"] == "applied"
    summary_id = r_sum["summary_id"]
    dn_loser = r_dn["loser_id"]

    # ── Invariance: the store predicate is BIT-IDENTICAL for every T < t_a ──
    for T in grid:
        assert T < t_a
        assert _visible_at(s, T) == before_sets[T], f"predicate changed at T={T}"

    # ── Intended deltas at T >= t_a ──
    # The summary is invisible before t_a and visible at/after it.
    assert summary_id not in _visible_at(s, t_a - 1)
    assert summary_id in _visible_at(s, t_a)
    # The retired near-dup loser keeps predicate visibility (valid_to untouched):
    # visible at any T after its own valid_at, INCLUDING at/after t_a.
    loser_row = s.get_fact(dn_loser)
    assert loser_row.is_latest == 0  # dropped from the latest surface
    assert loser_row.valid_to is None  # but as-of-visible
    assert dn_loser in _visible_at(s, t_a + 10 * DAY)

    # ── No inverted intervals anywhere post-apply ──
    inverted = s._db.execute(
        "SELECT id FROM fact WHERE valid_to IS NOT NULL AND valid_to <= valid_at"
    ).fetchall()
    assert inverted == [], "no inverted (valid_to <= valid_at) intervals post-apply"
    s.close()


def test_apply_path_grid_fails_if_summary_backdated(db_path, emb, monkeypatch):
    """Fault-injection guard for §10.1: if the summarize apply BACKDATED valid_at (used
    an old source time instead of t_a), the summary would leak into a past window and
    the T < t_a invariance would break. We patch the apply to backdate and assert the
    grid check would catch it — then the real code (unpatched) does not backdate."""
    from lean_memory.maintain import lifecycle as lc

    s = _seed(_open(db_path, emb), emb)
    sources = [
        _add(s, f"aged note {i}", predicate="notes", valid_at=T_A - (200 - i) * DAY)
        for i in range(3)
    ]
    run_id = _run_id(s)
    pid = _stage_summarize(s, run_id, sources, "Consolidated")

    grid = [T_A - t * DAY for t in (150, 100, 50, 1)]
    before = {T: _visible_at(s, T) for T in grid}

    # Fault-inject: force the summary's valid_at to a BACKDATED source time.
    backdate = T_A - 200 * DAY
    orig_add_fact = s.add_fact

    def _backdating_add_fact(fact, full, coarse):
        if fact.record_kind == "summary":
            fact.valid_at = backdate  # the bug we are guarding against
        return orig_add_fact(fact, full, coarse)

    monkeypatch.setattr(s, "add_fact", _backdating_add_fact)
    t_a = T_A + DAY
    lc.decide(s, emb, pid, "approve", now=t_a, decided_by="console")

    # With the injected backdate, the past-window predicate CHANGED — the grid test
    # would have caught the regression.
    changed = any(_visible_at(s, T) != before[T] for T in grid)
    assert changed, "fault injection did not perturb a past window — test is inert"
    s.close()


# ── Memory façade wiring (maintain / review_queue / decide / promote) ──────────
def _full_db_hash(store):
    out = {}
    for t in ("fact", "episode", "entity", "maintenance_proposal",
              "maintenance_run", "fact_derivation"):
        out[t] = [tuple(r) for r in store._db.execute(f"SELECT * FROM {t}").fetchall()]
    return hashlib.sha256(json.dumps(out, default=str, sort_keys=True).encode()).hexdigest()


def _facade_corpus(store, now):
    """A controlled corpus written straight to the namespace file so the façade tests
    are deterministic regardless of the offline extraction stub. Timestamps are anchored
    to `now` (the real wall clock the façade's runner uses) so the age gates pass —
    every fact is 300+ days old relative to the run's `now`."""
    for i in range(5):
        _add(store, f"aged note {i} about the trip", predicate="notes",
             valid_at=now - (300 - i) * DAY, salience=5.0)
    _add(store, "low value aged remark", predicate="chatter",
         valid_at=now - 300 * DAY, salience=1.0, access_count=2,
         last_access=now - 300 * DAY)


def test_facade_dry_run_writes_nothing(db_path, emb):
    """Memory.maintain(apply=False) computes the report but mutates NOTHING — the full
    DB is byte-identical, and no lease/proposal row is written (§7.1 dry-run)."""
    from lean_memory.memory import Memory

    # Seed the namespace file the Memory will open.
    from lean_memory.types import now_ms

    now = now_ms()
    s = _seed(_open(db_path, emb), emb)
    _facade_corpus(s, now)
    before = _full_db_hash(s)
    s.close()

    m = Memory(root=db_path.parent, embedder=emb)
    # The seeded facts live in namespace 'ns'; point the façade at the same file.
    ns = "ns"
    target = m._namespace_path(ns)
    import shutil

    shutil.copy(db_path, target)

    cfg = MaintenanceConfig(tau_near=0.0, min_cluster=2, age_floor_days=1,
                            min_new_facts=1, proposal_budget_per_run=100)
    rep = m.maintain(ns, config=cfg, apply=False)
    assert rep.status == "ok"
    assert rep.run_id is None  # dry-run takes no lease

    check = _open(target, emb)
    assert _full_db_hash(check) == before, "dry-run mutated the DB"
    check.close()
    m.close()


def test_facade_apply_then_queue_then_decide_and_promote(db_path, emb):
    """The end-to-end façade path: maintain(apply=True) stages, review_queue groups by
    entity, decide(approve) applies, promote() restores hot."""
    from lean_memory.memory import Memory

    from lean_memory.types import now_ms

    now = now_ms()
    s = _seed(_open(db_path, emb), emb)
    _facade_corpus(s, now)
    s.close()

    m = Memory(root=db_path.parent, embedder=emb)
    ns = "ns"
    import shutil

    shutil.copy(db_path, m._namespace_path(ns))

    cfg = MaintenanceConfig(tau_near=0.0, min_cluster=2, age_floor_days=1,
                            evict_threshold=0.9, auto_evict_salience=0.0,
                            auto_evict_age_days=100000, min_new_facts=1,
                            proposal_budget_per_run=100)
    rep = m.maintain(ns, config=cfg, apply=True)
    assert rep.status == "ok" and rep.run_id is not None

    groups = m.review_queue(ns, limit=50)
    assert groups, "review_queue returned no groups"
    # Every group is JSON-friendly with a proposals list carrying parsed payloads.
    all_props = [p for g in groups for p in g["proposals"]]
    assert all_props
    assert all("payload" in p for p in all_props)

    # Approve an evict proposal through the façade → the fact goes cold.
    evict = next(p for p in all_props if p["kind"] == "evict")
    fact_id = evict["payload"]["fact_id"]
    r = m.decide(evict["id"], "approve", namespace=ns)
    assert r["outcome"] == "applied"
    assert m._maintenance_store(ns).get_fact(fact_id).tier == "cold"

    # Promote it back through the façade.
    pr = m.promote(fact_id, namespace=ns)
    assert pr["outcome"] == "promoted"
    assert m._maintenance_store(ns).get_fact(fact_id).tier == "hot"
    m.close()


def test_facade_review_queue_lazy_timeout_expiry(db_path, emb):
    """review_queue expires any pending proposal past its expires_at (timeout) and does
    not return it (§5 lazy expiry)."""
    from lean_memory.memory import Memory

    ns = "ns"  # the seeded facts + proposal live in namespace 'ns'
    s = _seed(_open(db_path, emb), emb)
    fact = _add(s, "trivial ancient note", predicate="notes", salience=1.0)
    run_id = s.create_run(ns, "cli", T_A, "cfg")
    # Stage a proposal (in namespace `ns`) that already expired.
    payload = {"fact_id": fact.id, "fact_text": fact.fact_text, "value": 0.1,
               "salience": 1.0, "access_count": 0, "evidence_backend": "score"}
    # review_queue lazily expires against the real wall clock (now_ms), so the proposal
    # must expire in the REAL past — created_at/expires_at at epoch ~0 do exactly that.
    pid = s.stage_proposal(
        run_id=run_id, namespace=ns, kind="evict",
        payload_json=json.dumps(payload, sort_keys=True),
        created_at=1, expires_at=1,  # long expired against any real now_ms()
        evidence_backend="score",
    )
    s.close()

    m = Memory(root=db_path.parent, embedder=emb)
    import shutil

    shutil.copy(db_path, m._namespace_path(ns))

    groups = m.review_queue(ns, limit=50)
    assert groups == []  # the expired proposal is not surfaced
    row = m._maintenance_store(ns).get_proposal(pid)
    assert row["status"] == "expired" and row["expiry_reason"] == "timeout"
    m.close()

