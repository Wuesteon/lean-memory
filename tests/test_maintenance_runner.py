"""Task 5 — MaintenanceRunner: lease, work thresholds, cursor, crash-resume.

All offline (FakeEmbedder, deterministic stub summarizer) against real SqliteStores.
Pins design-spec §6.6 (thresholds + cursor), §7.2 (lease), §7.4 (crash safety):

  - Lease: fresh-lease clean skip; stale-heartbeat takeover marks 'aborted' + proceeds;
    a real two-process race (subprocess claims first → in-process runner skips);
    lost-race IntegrityError path.
  - Thresholds: below-threshold run is a cheap no-op (no transforms, lease released
    'ok', below_threshold stat); >=7-days-since-last-run triggers with 0 new facts;
    first-ever run triggers.
  - Cursor: run 1 finishes with cursor C1; a duplicate lands on a long-quiet slot;
    run 2 sees it via iter_slots_touched_since(C1) and dedups it (the cursor-gap case).
  - Cross-run exclusion: a fact referenced by a PRIOR run's pending proposal is not
    auto-demoted/deduped this run; identical-pending proposals are not double-staged.
  - Crash/resume: abandon a 'running' row with a stale heartbeat + partial work; the
    next runner takes over (aborts it), re-runs, and converges (no duplicate proposals,
    no double summary).
  - Heartbeat: heartbeat_at advances across batch boundaries during a run (observed
    via the on_batch hook + get_live_run mid-run).

Time is injected everywhere (clock / now_ms) so no test sleeps on real wall-clock.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.maintain import MaintenanceConfig, MaintenanceRunner
from lean_memory.maintain.config import MS_PER_DAY
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact

NOW = 2_000_000_000_000  # fixed wall-clock (epoch ms)
NOW_S = NOW / 1000.0

# A tiny config so tests trip thresholds with a handful of facts (defaults are 200).
def _cfg(**over):
    base = dict(
        min_new_facts=3,
        min_new_salience=1000.0,  # high, so the facts-count threshold is the live one
        max_days_between_runs=7,
        age_floor_days=90,
        min_cluster=5,
        proposal_budget_per_run=50,
    )
    base.update(over)
    return MaintenanceConfig(**base)


# ── clock injection helpers ───────────────────────────────────────────────────
class Clock:
    """A hand-cranked wall-clock (seconds). now_ms derives from it."""

    def __init__(self, t_s: float = NOW_S) -> None:
        self.t = t_s

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def now_ms(self) -> int:
        return int(self.t * 1000)


# ── fixtures / helpers ────────────────────────────────────────────────────────
def _make_store(path):
    emb = FakeEmbedder()
    s = SqliteStore(path, dim=emb.dim, coarse_dim=emb.coarse_dim)
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    s.add_episode(ep)
    subj = s.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    s._emb = emb
    s._ep = ep
    s._subj = subj
    return s


@pytest.fixture
def store(tmp_path):
    s = _make_store(tmp_path / "ns.db")
    yield s
    s.close()


def _add(
    store,
    text,
    *,
    predicate="works_at",
    valid_at,
    salience=5.0,
    access_count=0,
    last_access=None,
    subject_id=None,
    embed_text=None,
):
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
    )
    full, coarse = store._emb.embed_with_coarse(embed_text if embed_text is not None else text)
    store.add_fact(f, full, coarse)
    return f


def _runner(store, clock=None, **kw):
    clock = clock or Clock()
    return MaintenanceRunner(
        store, "ns", _cfg(**kw.pop("cfg", {})),
        clock=clock, now_ms=clock.now_ms, **kw
    )


# ══════════════════════════════════════════════════════════════════════════════
# Lease (§7.2)
# ══════════════════════════════════════════════════════════════════════════════
def test_second_runner_skips_while_fresh_lease_held(tmp_path):
    """A second store handle on the SAME file cleanly skips while the first holds a
    fresh lease (§7.2)."""
    s1 = _make_store(tmp_path / "ns.db")
    clock = Clock()
    # First runner claims and heartbeats — simulate a held lease by claiming directly.
    run_id = s1.create_run("ns", "cli", clock.now_ms(), _cfg().config_hash())
    s1.heartbeat_run(run_id, clock.now_ms())

    # Second store handle on the same path (busy_timeout=5000 for maintenance).
    s2 = SqliteStore(tmp_path / "ns.db", dim=s1._emb.dim, coarse_dim=s1._emb.coarse_dim,
                     busy_timeout_ms=5000)
    r2 = MaintenanceRunner(s2, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r2.run()
    assert rep.status == "skipped"
    assert rep.skipped_reason == "lease held"
    # The first lease is untouched (still running).
    assert s1.get_live_run("ns")["id"] == run_id
    s1.close()
    s2.close()


def test_stale_heartbeat_takeover_marks_aborted_and_proceeds(store):
    """A live run with a STALE heartbeat is marked 'aborted' and taken over (§7.2)."""
    clock = Clock()
    # Plant a live run with an OLD heartbeat (older than the 5-min stale floor).
    stale_id = store.create_run("ns", "cli", clock.now_ms(), _cfg().config_hash())
    store.heartbeat_run(stale_id, clock.now_ms())
    clock.advance(10 * 60)  # 10 minutes — past the 300s floor

    _add(store, "a", valid_at=NOW - 400 * MS_PER_DAY)
    _add(store, "b", valid_at=NOW - 400 * MS_PER_DAY)
    _add(store, "c", valid_at=NOW - 400 * MS_PER_DAY)

    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    assert rep.status == "ok"
    assert rep.took_over_run_id == stale_id
    # The stale run is now 'aborted', not 'running'.
    aborted = store._db.execute(
        "SELECT status FROM maintenance_run WHERE id=?", (stale_id,)
    ).fetchone()["status"]
    assert aborted == "aborted"
    # Our own run finished 'ok'.
    assert store._db.execute(
        "SELECT status FROM maintenance_run WHERE id=?", (rep.run_id,)
    ).fetchone()["status"] == "ok"


def test_stale_takeover_never_rolls_back_prior_work(store):
    """Takeover marks the stale run aborted but never rolls back its committed work
    (§7.2/§7.4) — a proposal staged by the crashed run survives."""
    clock = Clock()
    stale_id = store.create_run("ns", "cli", clock.now_ms(), _cfg().config_hash())
    store.heartbeat_run(stale_id, clock.now_ms())
    # The crashed run had staged a proposal before dying.
    pid = store.stage_proposal(
        run_id=stale_id, namespace="ns", kind="evict",
        payload_json=json.dumps({"fact_id": "zzz", "evidence_backend": "score"}),
        created_at=clock.now_ms(), expires_at=clock.now_ms() + MS_PER_DAY,
    )
    clock.advance(10 * 60)

    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    r.run()
    # The prior proposal is untouched — takeover rolled nothing back.
    assert store.get_proposal(pid)["status"] == "pending"


def test_two_process_lease_race_subprocess_claims_first(tmp_path):
    """A real subprocess claims the lease on a shared temp DB first; the in-process
    runner then cleanly skips (§7.2). Deterministic: the child claims before we try."""
    db_path = tmp_path / "ns.db"
    # Build the DB (schema + seed) in-process so the child only claims the lease.
    s = _make_store(db_path)
    s.close()

    # Child claims a live lease and heartbeats, WITHOUT finishing — it leaves the row
    # 'running' with a fresh heartbeat, then exits. The open row is the held lease.
    child = (
        "import sys;"
        "from lean_memory.store.sqlite_store import SqliteStore;"
        f"s=SqliteStore({str(db_path)!r}, busy_timeout_ms=5000);"
        "rid=s.create_run('ns','cli',2000000000000, None);"
        "s.heartbeat_run(rid, 2000000000000);"
        "s.close();"
        "print(rid)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    child_run_id = proc.stdout.strip()
    assert child_run_id

    # In-process runner: same wall-clock as the child's heartbeat → lease is fresh.
    s2 = _make_store(db_path)
    clock = Clock(t_s=NOW_S)  # equals the child's heartbeat time → fresh
    r = MaintenanceRunner(s2, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    assert rep.status == "skipped"
    assert rep.skipped_reason == "lease held"
    assert s2.get_live_run("ns")["id"] == child_run_id
    s2.close()


def test_lost_race_integrity_error_path(store, monkeypatch):
    """If a concurrent claimer inserts a live row between our check and INSERT, the
    create_run IntegrityError surfaces as 'lost race' (§7.2)."""
    import sqlite3

    clock = Clock()
    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)

    # get_live_run says 'nobody' (None), but create_run raises IntegrityError — exactly
    # the race window where another process claimed between check and INSERT.
    monkeypatch.setattr(store, "get_live_run", lambda ns: None)
    def _boom(*a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: ux_run_live")
    monkeypatch.setattr(store, "create_run", _boom)

    rep = r.run()
    assert rep.status == "skipped"
    assert rep.skipped_reason == "lost race"


def test_lease_claim_is_one_transaction_abort_rolls_back_on_failed_insert(store, monkeypatch):
    """The §7.2 sequence (check → stale-abort → INSERT) is ONE transaction: if the claim
    INSERT fails (IntegrityError), the stale-abort UPDATE in the same transaction MUST
    roll back too — the prior run stays 'running', not stranded 'aborted'.

    This is the behavioral fingerprint of single-transaction semantics: three separate
    autocommit statements would leave the prior run permanently aborted (a lost lease).
    """
    import sqlite3

    clock = Clock()
    # A stale live run (heartbeat 10 min old → past the 300s floor).
    stale_id = store.create_run("ns", "cli", clock.now_ms(), _cfg().config_hash())
    store.heartbeat_run(stale_id, clock.now_ms())
    clock.advance(10 * 60)

    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    # The claim INSERT loses a race (a concurrent process claimed) → IntegrityError.
    def _boom(*a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: ux_run_live")
    monkeypatch.setattr(store, "create_run", _boom)

    rep = r.run()
    assert rep.status == "skipped"
    assert rep.skipped_reason == "lost race"
    # The stale run's abort was rolled back with the failed INSERT — still 'running'.
    status = store._db.execute(
        "SELECT status FROM maintenance_run WHERE id=?", (stale_id,)
    ).fetchone()["status"]
    assert status == "running", "abort+claim not atomic — the abort UPDATE leaked"


# ══════════════════════════════════════════════════════════════════════════════
# Work thresholds (§6.6)
# ══════════════════════════════════════════════════════════════════════════════
def _finish_a_prior_run(store, clock, cursor_id, at_ms):
    """Record a finished 'ok' run with a given cursor + finished_at (test scaffold)."""
    rid = store.create_run("ns", "cli", at_ms, _cfg().config_hash())
    store.finish_run(rid, "ok", at_ms, json.dumps({}), cursor_id)
    return rid


def test_below_threshold_is_clean_noop(store):
    """Below all thresholds: no transforms, no proposals, lease released 'ok' with a
    below_threshold stat (§6.6/§10.9)."""
    clock = Clock()
    # A prior run finished recently with a cursor at the current high-water.
    _add(store, "seed", valid_at=NOW - 400 * MS_PER_DAY)
    hw = None
    for f in store.iter_latest_facts():
        hw = f.id
    _finish_a_prior_run(store, clock, hw, clock.now_ms())

    # No new facts since the cursor; recent last run → below all thresholds.
    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    assert rep.status == "ok"
    assert rep.below_threshold is True
    assert rep.threshold_stats.get("below_threshold") is True
    assert rep.transform_report is None  # no transforms invoked
    # Zero new proposals.
    assert store.list_proposals("ns", status="pending") == []
    # Lease released.
    assert store.get_live_run("ns") is None


def test_age_threshold_triggers_with_zero_new_facts(store):
    """>= max_days_between_runs since the last finished run triggers even with 0 new
    facts (§6.6)."""
    clock = Clock()
    _add(store, "seed", valid_at=NOW - 400 * MS_PER_DAY)
    hw = list(store.iter_latest_facts())[-1].id
    # Prior run finished 8 days ago (> max_days_between_runs=7).
    _finish_a_prior_run(store, clock, hw, clock.now_ms() - 8 * MS_PER_DAY)

    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    assert rep.status == "ok"
    assert rep.below_threshold is False
    assert rep.threshold_stats["triggered_by_age"] is True
    assert rep.threshold_stats["new_facts"] == 0


def test_first_ever_run_triggers(store):
    """The first-ever run (no prior finished run) is always over threshold (§6.6)."""
    clock = Clock()
    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    assert rep.status == "ok"
    assert rep.below_threshold is False
    assert rep.threshold_stats.get("reason") == "first_run"


def test_facts_count_threshold_triggers(store):
    """facts since cursor >= min_new_facts triggers (§6.6)."""
    clock = Clock()
    _add(store, "seed", valid_at=NOW - 400 * MS_PER_DAY)
    hw = list(store.iter_latest_facts())[-1].id
    _finish_a_prior_run(store, clock, hw, clock.now_ms())  # recent, so age won't trip

    # min_new_facts=3 → add 3 new facts on distinct slots.
    _add(store, "n1", predicate="p1", valid_at=NOW)
    _add(store, "n2", predicate="p2", valid_at=NOW)
    _add(store, "n3", predicate="p3", valid_at=NOW)

    r = MaintenanceRunner(store, "ns", _cfg(min_new_salience=1e9), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    assert rep.below_threshold is False
    assert rep.threshold_stats["triggered_by_facts"] is True
    assert rep.threshold_stats["new_facts"] == 3


# ══════════════════════════════════════════════════════════════════════════════
# Cursor semantics (§6.6) — the verified cursor-gap scenario
# ══════════════════════════════════════════════════════════════════════════════
def test_cursor_gap_duplicate_on_quiet_slot_deduped_next_run(store):
    """Run 1 finishes with cursor C1; a duplicate lands on a long-quiet slot; run 2
    sees that slot via iter_slots_touched_since(C1) and dedups it (§6.6)."""
    clock = Clock()
    # A quiet slot with one old fact, seeded before run 1.
    orig = _add(store, "Acme Corp", predicate="works_at", valid_at=NOW - 400 * MS_PER_DAY)

    # Run 1 (first-ever): processes the corpus, records cursor C1 = current high-water.
    r1 = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep1 = r1.run()
    assert rep1.status == "ok"
    c1 = rep1.cursor_id
    assert c1 is not None

    # A duplicate lands on that SAME long-quiet slot AFTER run 1 (id > C1).
    clock.advance(8 * 24 * 3600)  # 8 days later so the age threshold also trips
    dup = _add(store, "Acme Corp", predicate="works_at", valid_at=clock.now_ms())
    assert dup.id > c1  # newer id than the run-1 high-water

    # Run 2: the duplicate's slot must be re-scanned via iter_slots_touched_since(C1)
    # and the exact duplicate retired. Before: 2 latest in the slot; after: 1.
    before = [f for f in store.find_latest_in_slot(orig.subject_id, "works_at")]
    assert len(before) == 2

    r2 = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep2 = r2.run()
    assert rep2.status == "ok"
    after = [f for f in store.find_latest_in_slot(orig.subject_id, "works_at")]
    assert len(after) == 1, "the cursor-gap duplicate was deduped"
    assert after[0].id == orig.id  # survivor = argmin(valid_at) = the original


def test_below_threshold_noop_preserves_cursor_so_quiet_dup_survives(store):
    """A below-threshold no-op must NOT advance the cursor (§6.6 regression).

    Otherwise it strands an un-processed duplicate on a quiet slot: run 1 dedups with
    cursor C1; a duplicate D lands on quiet slot Q (id > C1); a below-threshold run
    would advance the cursor to C2 > D.id without processing Q; a later real run scans
    iter_slots_touched_since(C2) and NEVER sees Q. Preserving C1 across the no-op keeps
    Q in the next real run's scan window, so D is eventually deduped.
    """
    clock = Clock()
    orig = _add(store, "Acme Corp", predicate="works_at", valid_at=NOW - 400 * MS_PER_DAY)

    # Run 1 (first-ever, over threshold): finishes with cursor C1 = current high-water.
    r1 = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep1 = r1.run()
    assert rep1.status == "ok"
    c1 = rep1.cursor_id

    # A single duplicate lands on the quiet slot AFTER C1 — one new fact is BELOW the
    # facts threshold (min_new_facts=3) and salience is capped out, so the very next run
    # is a no-op.
    dup = _add(store, "Acme Corp", predicate="works_at", valid_at=clock.now_ms())
    assert dup.id > c1

    # The below-threshold no-op run: recent last run (age won't trip), 1 new fact
    # (< min_new_facts), high salience floor (won't trip).
    r2 = MaintenanceRunner(store, "ns", _cfg(min_new_salience=1e12), clock=clock, now_ms=clock.now_ms)
    rep2 = r2.run()
    assert rep2.status == "ok"
    assert rep2.below_threshold is True
    # THE FIX: the no-op preserved the last work-doing run's cursor (C1), not the
    # post-D high-water — so slot Q stays in the next run's scan window.
    assert rep2.cursor_id == c1
    # The duplicate is still un-deduped (the no-op did no work).
    assert len(store.find_latest_in_slot(orig.subject_id, "works_at")) == 2

    # A later over-threshold run (age trips after 8 days) MUST still see slot Q via
    # iter_slots_touched_since(C1) and dedup D.
    clock.advance(8 * 24 * 3600)
    r3 = MaintenanceRunner(store, "ns", _cfg(min_new_salience=1e12), clock=clock, now_ms=clock.now_ms)
    rep3 = r3.run()
    assert rep3.status == "ok"
    assert rep3.below_threshold is False
    after = store.find_latest_in_slot(orig.subject_id, "works_at")
    assert len(after) == 1, "the quiet-slot duplicate survived the no-op and was deduped"
    assert after[0].id == orig.id


def test_cursor_advances_before_transform_output(store):
    """The stored cursor is snapshotted BEFORE transform outputs are written: it equals
    the pre-run max fact id, never a summary/maintenance-episode id created mid-run."""
    clock = Clock()
    pre_ids = [f.id for f in store.iter_latest_facts()]
    _add(store, "x", valid_at=NOW - 400 * MS_PER_DAY)
    hw = list(store.iter_latest_facts())[-1].id

    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    # Cursor is the pre-write high-water — the id of the last fact that existed at start.
    assert rep.cursor_id == hw


# ══════════════════════════════════════════════════════════════════════════════
# Cross-run exclusion (§4.4) + identical-pending dedupe guard (§7.4)
# ══════════════════════════════════════════════════════════════════════════════
def test_fact_in_prior_pending_proposal_not_auto_demoted(store):
    """A fact referenced by a PRIOR run's pending EVICT proposal is not auto-demoted
    this run (§4.4 cross-run exclusion)."""
    clock = Clock()
    # An auto-band-eligible fact: salience<2, access_count=0, very old.
    victim = _add(
        store, "low value old fact", predicate="note",
        valid_at=NOW - 400 * MS_PER_DAY, salience=0.0, access_count=0,
    )
    # A prior run staged a pending EVICT proposal that references this exact fact.
    prior_run = store.create_run("ns", "cli", clock.now_ms() - MS_PER_DAY, _cfg().config_hash())
    store.finish_run(prior_run, "ok", clock.now_ms() - MS_PER_DAY, json.dumps({}), None)
    store.stage_proposal(
        run_id=prior_run, namespace="ns", kind="evict",
        payload_json=json.dumps({"fact_id": victim.id, "evidence_backend": "score"}),
        created_at=clock.now_ms() - MS_PER_DAY, expires_at=clock.now_ms() + 10 * MS_PER_DAY,
    )

    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    assert rep.status == "ok"
    # The victim was NOT auto-demoted — its tier is still hot.
    tier = store._db.execute("SELECT tier FROM fact WHERE id=?", (victim.id,)).fetchone()["tier"]
    assert tier == "hot", "a fact under review is never auto-demoted"
    assert victim.id not in (rep.transform_report.demoted_ids if rep.transform_report else [])


def test_identical_pending_dedup_near_not_double_staged(store):
    """A dedup_near proposal identical to one already pending (same fact-id pair) is not
    re-staged on the next run — even when the slot is RE-SCANNED (§7.4 idempotence).

    The guard is load-bearing precisely when run 2 revisits the pair: we land a third
    member on the same slot after run 1's cursor so iter_slots_touched_since re-surfaces
    it, forcing dedup_near to re-evaluate the a/b pair. The skip-signature is then the
    only thing preventing a second identical proposal.
    """
    clock = Clock()
    # Two near-duplicate facts in one slot (same forced embedding, different text →
    # cosine 1.0 >= tau, but not textually identical → DEDUP-NEAR, a proposal).
    a = _add(store, "salary is 100k", predicate="salary", valid_at=NOW - 400 * MS_PER_DAY,
             embed_text="SALARY")
    b = _add(store, "earns 100000", predicate="salary", valid_at=NOW - 399 * MS_PER_DAY,
             embed_text="SALARY")

    # Run 1 stages the near-dup proposal for the a/b pair.
    r1 = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep1 = r1.run()
    pending1 = store.list_proposals("ns", status="pending", kind="dedup_near")
    assert len(pending1) == 1
    payload = json.loads(pending1[0]["payload_json"])
    assert set(payload["fact_ids"]) == {a.id, b.id}

    # A THIRD, textually-distinct member lands on the SAME slot after C1 — this both
    # trips the age/facts threshold AND re-touches the slot so run 2 re-scans it.
    clock.advance(8 * 24 * 3600)
    c = _add(store, "compensation 100k", predicate="salary", valid_at=clock.now_ms(),
             embed_text="SALARY")
    assert c.id > rep1.cursor_id

    r2 = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    r2.run()
    pending2 = store.list_proposals("ns", status="pending", kind="dedup_near")
    # The a/b pair is NOT re-staged (skip-signature); only NEW pairs involving c are.
    ab = frozenset((a.id, b.id))
    ab_count = sum(
        1 for p in pending2 if frozenset(json.loads(p["payload_json"])["fact_ids"]) == ab
    )
    assert ab_count == 1, "identical near-dup evidence is not double-staged"


# ══════════════════════════════════════════════════════════════════════════════
# Crash / resume (§7.4)
# ══════════════════════════════════════════════════════════════════════════════
def test_crash_resume_converges_no_duplicate_proposals(store):
    """Simulate a crash: a 'running' row with a stale heartbeat + a partially-staged
    proposal, abandoned WITHOUT finish_run. The next runner takes over (aborts it),
    re-runs, and converges — no duplicate proposals for the same evidence (§7.4)."""
    clock = Clock()
    a = _add(store, "salary is 100k", predicate="salary", valid_at=NOW - 400 * MS_PER_DAY,
             embed_text="SALARY")
    b = _add(store, "earns 100000", predicate="salary", valid_at=NOW - 399 * MS_PER_DAY,
             embed_text="SALARY")

    # The crashed run: claimed the lease, staged the near-dup proposal, then died —
    # left 'running' with an OLD heartbeat (no finish_run).
    crashed = store.create_run("ns", "cli", clock.now_ms(), _cfg().config_hash())
    store.heartbeat_run(crashed, clock.now_ms())
    store.stage_proposal(
        run_id=crashed, namespace="ns", kind="dedup_near",
        payload_json=json.dumps({
            "slot": {"subject_id": a.subject_id, "predicate": "salary"},
            "fact_ids": [a.id, b.id],
            "fact_texts": {a.id: a.fact_text, b.id: b.fact_text},
            "cosine": 1.0, "multivalued": False,
            "proposed_survivor": a.id, "evidence_backend": "stored",
        }),
        created_at=clock.now_ms(), expires_at=clock.now_ms() + 30 * MS_PER_DAY,
    )
    clock.advance(10 * 60)  # heartbeat now stale (> 300s floor)

    # The recovery runner takes over.
    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep = r.run()
    assert rep.status == "ok"
    assert rep.took_over_run_id == crashed
    # The crashed run is 'aborted'; its staged proposal survives (never rolled back).
    assert store._db.execute(
        "SELECT status FROM maintenance_run WHERE id=?", (crashed,)
    ).fetchone()["status"] == "aborted"
    # Convergence: exactly ONE dedup_near proposal for this pair — no double-stage.
    near = store.list_proposals("ns", status="pending", kind="dedup_near")
    assert len(near) == 1


def test_crash_mid_run_leaves_consistent_db_and_marks_aborted(store):
    """A crash DURING the transform phase (raised at a batch boundary via on_batch)
    leaves a consistent DB; the runner marks its own row aborted before re-raising, and
    the next runner takes over (§7.4)."""
    clock = Clock()
    # Exact-dup cluster so dedup_exact runs a batch we can crash inside.
    _add(store, "Acme", predicate="works_at", valid_at=NOW - 400 * MS_PER_DAY)
    _add(store, "acme", predicate="works_at", valid_at=NOW - 399 * MS_PER_DAY)  # exact-dup

    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)

    boom_calls = {"n": 0}

    def _crash_on_second_batch():
        boom_calls["n"] += 1
        if boom_calls["n"] >= 2:  # let ≥1 batch commit, then crash at a boundary
            raise RuntimeError("simulated crash mid-run")

    with pytest.raises(RuntimeError, match="simulated crash"):
        r.run(on_batch=_crash_on_second_batch)

    # The runner marked its own row 'aborted' (no lingering 'running' lease it owns).
    live = store.get_live_run("ns")
    assert live is None, "crashed runner released/aborted its own live row"

    # The DB is consistent — no inverted intervals from the partial run.
    bad = store._db.execute(
        "SELECT COUNT(*) c FROM fact WHERE valid_to IS NOT NULL AND valid_to < valid_at"
    ).fetchone()["c"]
    assert bad == 0

    # A fresh runner takes over cleanly and finishes ok.
    clock.advance(1.0)
    r2 = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)
    rep2 = r2.run()
    assert rep2.status == "ok"


# ══════════════════════════════════════════════════════════════════════════════
# Heartbeat cadence (§7.2)
# ══════════════════════════════════════════════════════════════════════════════
def test_heartbeat_advances_across_batch_boundaries(store):
    """heartbeat_at advances across batch boundaries during a run — observed via the
    on_batch hook reading get_live_run mid-run (§7.2)."""
    clock = Clock()
    # A corpus with multiple exact-dup clusters on distinct slots → multiple batches.
    for i in range(3):
        pred = f"slot{i}"
        _add(store, "DupVal", predicate=pred, valid_at=NOW - 400 * MS_PER_DAY)
        _add(store, "dupval", predicate=pred, valid_at=NOW - 399 * MS_PER_DAY)  # exact-dup

    r = MaintenanceRunner(store, "ns", _cfg(), clock=clock, now_ms=clock.now_ms)

    seen_heartbeats = []

    def _observe():
        # Each batch boundary advances the clock a hair, then the runner has already
        # heartbeated; capture the live row's heartbeat_at as observed mid-run.
        clock.advance(0.01)
        live = store.get_live_run("ns")
        if live is not None:
            seen_heartbeats.append(live["heartbeat_at"])

    rep = r.run(on_batch=_observe)
    assert rep.status == "ok"
    # We crossed >= 2 batch boundaries and the heartbeat strictly advanced.
    assert len(seen_heartbeats) >= 2
    assert seen_heartbeats == sorted(seen_heartbeats)
    assert seen_heartbeats[-1] > seen_heartbeats[0]
