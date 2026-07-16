"""MaintenanceRunner — the concurrency-and-crash-safety driver (design spec §6.6, §7).

One runner drives ONE namespace store through a single maintenance run:

  1. LEASE (§7.2): the `maintenance_run` INSERT is the atomic claim — the partial
     unique index `ux_run_live` makes a second live run for the same namespace raise
     IntegrityError, never a silent second row. Before claiming, a live run with a
     FRESH heartbeat means we cleanly skip; a live run with a STALE heartbeat is
     marked 'aborted' and taken over. Takeover NEVER rolls anything back — transforms
     are idempotent and every batch co-commits (§7.4), so a partial prior run is safe
     to resume from DB state.
  2. WORK THRESHOLDS (§6.6, OR-combined): facts since cursor >= min_new_facts, OR
     cumulative salience of new facts >= min_new_salience, OR >= max_days_between_runs
     since the last finished run. Below all of them the run is a cheap no-op: it
     releases the lease with status 'ok' and a `below_threshold` stat, writing no
     proposals and invoking no transforms. The first-ever run (no prior finished run)
     is always over threshold.
  3. CURSOR (§6.6, advance-before-write): the new cursor is the max fact id snapshotted
     at run START, BEFORE any transform output is written — so the run's own outputs
     (summaries with higher ids, maintenance episodes) land after it and are excluded
     from the next run's "new facts" delta by id order. Slot-level transforms are
     driven by `iter_slots_touched_since(previous_cursor)`; `iter_latest_facts` feeds
     evict/summarize. Candidate scans already exclude record_kind='summary' (verified
     in transforms.py — we rely on it, never duplicate it).
  4. CROSS-RUN EXCLUSION (§4.4): the auto phase excludes not just this run's staged
     ids but every fact id referenced by a PRIOR run's still-pending proposal, and a
     propose transform never re-stages an identical-evidence proposal (§7.4).

Heartbeats fire at every batch boundary via the `on_batch` hook the runner threads
into the store, and the runner heartbeats around the transform phase. The staleness
threshold is `max(300s, 10 * longest observed single-batch duration this process)` —
`observed` defaults to 0, so a fresh process uses the 5-minute floor; nothing exotic
is persisted.

Store construction (§7.1): the runner does NOT construct stores — the CLI/Memory
wiring (Tasks 6-7) opens a dedicated maintenance `SqliteStore` with
`busy_timeout_ms=5000` for the run's duration and passes it in. The runner only
drives whatever store it is handed. Tests open that second/maintenance store directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..store.base import Store
from .config import MS_PER_DAY, MaintenanceConfig
from .summarize import Summarizer
from .transforms import TransformReport, run_transforms

# Staleness floor: a live run whose heartbeat is older than this (or older than
# 10x the longest observed single-batch duration, whichever is larger) is abandoned
# and may be taken over (§7.2).
_STALE_FLOOR_S = 300.0  # 5 minutes


@dataclass
class RunReport:
    """The outcome of one `MaintenanceRunner.run()` — a small, JSON-able record.

    `status` is one of:
      - 'ok'      : the run claimed the lease, did its work (possibly a no-op), and
                    released the lease cleanly.
      - 'skipped' : the run did NOT claim the lease; `skipped_reason` says why.
    Note the ledger row's own status is 'ok'|'aborted'|'running'; a 'skipped' RunReport
    writes no ledger row (it never held the lease).
    """

    status: str
    run_id: Optional[str] = None
    skipped_reason: Optional[str] = None
    below_threshold: bool = False
    threshold_stats: dict = field(default_factory=dict)
    transform_report: Optional[TransformReport] = None
    cursor_id: Optional[str] = None
    config_hash: Optional[str] = None
    took_over_run_id: Optional[str] = None  # id of a stale run this run aborted, if any


def _heartbeat_is_fresh(live_run: dict, now_s: float, stale_after_s: float) -> bool:
    """True iff the live run's heartbeat is within `stale_after_s` of `now_s`.

    A run with NO heartbeat_at (shouldn't happen — create_run stamps started_at into
    it — but be defensive) is treated as stale so a wedged run can always be reclaimed.
    """
    hb = live_run.get("heartbeat_at")
    if hb is None:
        return False
    age_s = now_s - (hb / 1000.0)
    return age_s < stale_after_s


def _proposal_fact_ids(proposal: dict) -> list[str]:
    """Parse the fact ids a pending proposal references, kind-aware (§4.4/§5 payloads).

    Payload shapes differ by kind:
      - dedup_near : payload['fact_ids']       -> [a, b]
      - summarize  : payload['source_fact_ids']-> [...]
      - evict      : payload['fact_id']        -> single id
    A malformed/absent payload yields [] (defensive — never crash the exclusion scan).
    """
    try:
        payload = json.loads(proposal["payload_json"])
    except (KeyError, TypeError, ValueError):
        return []
    kind = proposal.get("kind")
    if kind == "dedup_near":
        return list(payload.get("fact_ids", []))
    if kind == "summarize":
        return list(payload.get("source_fact_ids", []))
    if kind == "evict":
        fid = payload.get("fact_id")
        return [fid] if fid else []
    # Unknown kind: union whatever id-ish keys are present, defensively.
    ids: list[str] = []
    ids.extend(payload.get("fact_ids", []))
    ids.extend(payload.get("source_fact_ids", []))
    if payload.get("fact_id"):
        ids.append(payload["fact_id"])
    return ids


class MaintenanceRunner:
    """Drives one maintenance run on ONE namespace store (design spec §6.6, §7).

    Stateful only in `_max_observed_batch_s` — the longest single-batch duration this
    process has seen, feeding the staleness threshold (§7.2). Nothing exotic is
    persisted; a fresh process starts at 0 and uses the 5-minute floor.
    """

    def __init__(
        self,
        store: Store,
        namespace: str,
        config: Optional[MaintenanceConfig] = None,
        *,
        trigger: str = "cli",
        summarizer: Optional[Summarizer] = None,
        clock: Optional[Callable[[], float]] = None,
        now_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.config = config or MaintenanceConfig()
        self.trigger = trigger
        self.summarizer = summarizer
        # `clock` is wall-clock seconds (for heartbeat freshness/staleness); `now_ms`
        # is world/wall time in epoch ms (for `valid_at` deltas + ledger timestamps).
        # Both injectable so tests never sleep on real time.
        self._clock = clock or time.time
        self._now_ms = now_ms or (lambda: int(self._clock() * 1000))
        # Longest single-batch duration observed this process (seconds); feeds the
        # staleness threshold. Persisted nowhere — a fresh process starts at 0.
        self._max_observed_batch_s = 0.0

    # ── staleness threshold (§7.2) ──
    def _stale_after_s(self) -> float:
        """max(5 min, 10x longest observed single-batch duration this process)."""
        return max(_STALE_FLOOR_S, 10.0 * self._max_observed_batch_s)

    # ── cursor high-water (§6.6, advance-before-write) ──
    def _max_fact_id(self) -> Optional[str]:
        """Max fact id among currently-latest facts — the pre-write cursor high-water.

        Snapshotted at run START, before any transform writes. Ids are time-sortable
        (types.new_id), so `iter_latest_facts` yields in id order and the last is the
        max. The run's own later outputs (summaries, maintenance episodes) get HIGHER
        ids and thus fall after this cursor — excluded from the next run's delta by id
        order, as §6.6 requires. None on an empty namespace.
        """
        last: Optional[str] = None
        for f in self.store.iter_latest_facts():
            last = f.id
        return last

    # ── work thresholds (§6.6) ──
    def _threshold_check(self, previous_cursor: Optional[str], last_finished_at: Optional[int]):
        """Decide whether this run is over threshold (§6.6), returning (over, stats).

        Over threshold iff ANY of (OR):
          - facts since `previous_cursor` >= config.min_new_facts
          - cumulative salience of those new facts >= config.min_new_salience
          - >= config.max_days_between_runs since the last finished run
        The first-ever run (previous_cursor is None) is unconditionally over threshold
        — there is no cursor yet and nothing to gate on.
        """
        if previous_cursor is None:
            return True, {"reason": "first_run"}

        new_facts = 0
        new_salience = 0.0
        for f in self.store.iter_latest_facts(after_id=previous_cursor):
            if f.record_kind == "summary":
                continue  # the job's own outputs never count as new work (§6.6)
            new_facts += 1
            new_salience += f.salience

        now_ms = self._now_ms()
        days_since = (
            (now_ms - last_finished_at) / MS_PER_DAY if last_finished_at is not None else None
        )

        by_facts = new_facts >= self.config.min_new_facts
        by_salience = new_salience >= self.config.min_new_salience
        by_age = days_since is not None and days_since >= self.config.max_days_between_runs

        stats = {
            "new_facts": new_facts,
            "new_salience": round(new_salience, 6),
            "days_since_last_run": round(days_since, 6) if days_since is not None else None,
            "triggered_by_facts": by_facts,
            "triggered_by_salience": by_salience,
            "triggered_by_age": by_age,
        }
        return (by_facts or by_salience or by_age), stats

    # ── cross-run exclusion (§4.4) ──
    def _prior_pending(self):
        """(exclude_ids, pending_signatures) from prior-run still-pending proposals.

        exclude_ids: union of every fact id referenced by a pending proposal — the
        auto phase must not touch a fact a human is still reviewing.
        pending_signatures: {kind: {frozenset(fact_ids)}} so a propose transform can
        skip re-staging an identical-evidence proposal (crash idempotence, §7.4).
        """
        exclude_ids: set[str] = set()
        signatures: dict[str, set[frozenset[str]]] = {}
        for p in self.store.list_proposals(self.namespace, status="pending", limit=1_000_000):
            fids = _proposal_fact_ids(p)
            exclude_ids.update(fids)
            kind = p.get("kind")
            if kind:
                signatures.setdefault(kind, set()).add(frozenset(fids))
        return exclude_ids, signatures

    # ── lease (§7.2) ──
    def _try_claim_lease(self, now_ms: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Attempt the atomic lease claim. Returns (run_id, skipped_reason, took_over).

        - Fresh live lease held  -> (None, 'lease held', None): clean skip.
        - Stale live lease       -> abort it, then claim: (run_id, None, aborted_id).
        - No live lease          -> claim: (run_id, None, None).
        - Lost race on INSERT     -> (None, 'lost race', None): a concurrent claimer won.

        Spec §7.2 binds the whole sequence as ONE transaction:
        `BEGIN IMMEDIATE → check live-heartbeat row → (optional) abort stale → INSERT
        (loser hits ux_run_live) → COMMIT`. `store.batch()` supplies the BEGIN IMMEDIATE
        and single COMMIT, suppressing the CRUD verbs' per-call commits so
        get_live_run/finish_run/create_run all land in the one transaction. The
        create_run INSERT is the atomic claim; a second live INSERT raises
        IntegrityError — batch's __exit__ rolls the whole transaction back first, THEN
        we catch it OUTSIDE the batch context and return the lost-race skip.
        """
        import sqlite3

        # Fresh-lease skip must NOT open a write transaction — read first, and only if a
        # takeover/claim is actually warranted do we enter BEGIN IMMEDIATE. The batch
        # below re-reads under the write lock (the authoritative §7.2 check); this
        # pre-check just short-circuits the common held-lease case cheaply.
        live = self.store.get_live_run(self.namespace)
        if live is not None and _heartbeat_is_fresh(
            live, now_ms / 1000.0, self._stale_after_s()
        ):
            return None, "lease held", None

        # One transaction (§7.2): BEGIN IMMEDIATE → check → optional stale-abort →
        # INSERT claim → COMMIT. batch() suppresses the CRUD verbs' per-call commits so
        # they co-commit; IntegrityError on the INSERT is caught OUTSIDE the batch, after
        # __exit__ has rolled the whole transaction back.
        result: dict = {}
        try:
            with self.store.batch():
                live = self.store.get_live_run(self.namespace)
                if live is not None:
                    if _heartbeat_is_fresh(
                        live, now_ms / 1000.0, self._stale_after_s()
                    ):
                        # Became fresh between the pre-check and the lock: abandon the
                        # claim (the write-free transaction commits empty on return).
                        return None, "lease held", None
                    # Stale: mark it 'aborted' in the SAME transaction. Takeover NEVER
                    # rolls anything back (§7.2/§7.4) — the prior run's own work is
                    # already durably committed; this only clears status='running' to
                    # release ux_run_live for the claim below.
                    self.store.finish_run(
                        live["id"], "aborted", now_ms, None, live.get("cursor_id")
                    )
                    result["took_over"] = live["id"]
                # The atomic claim: a concurrent live row makes this raise IntegrityError.
                result["run_id"] = self.store.create_run(
                    self.namespace, self.trigger, now_ms, self.config.config_hash()
                )
        except sqlite3.IntegrityError:
            # __exit__ already rolled back the whole transaction (including any
            # stale-abort UPDATE), leaving the concurrent claimer's live row intact.
            return None, "lost race", None
        return result["run_id"], None, result.get("took_over")

    # ── dry run (§6.3 — report-only, zero writes, no lease) ──
    def dry_run(self) -> RunReport:
        """Compute the full would-do report with ZERO writes and NO lease (§6.3).

        A dry-run mutates nothing — no proposals, no auto transforms, not even a lease
        row (it writes nothing, so it needs no lease). It still runs the threshold gate
        and, if over threshold, the transforms in report-only mode (`dry_run=True`
        suppresses every store write and counts instead). The returned RunReport has
        status 'ok' and run_id=None (no ledger row exists). This is the `apply=False`
        default path for `Memory.maintain()` and the CLI/MCP tools.
        """
        cfg_hash = self.config.config_hash()
        prior = self.store.last_finished_run(self.namespace)
        previous_cursor = prior.get("cursor_id") if prior else None
        last_finished_at = prior.get("finished_at") if prior else None

        over, threshold_stats = self._threshold_check(previous_cursor, last_finished_at)
        stats = dict(threshold_stats)
        if not over:
            stats["below_threshold"] = True
            return RunReport(
                status="ok", run_id=None, below_threshold=True,
                threshold_stats=stats, transform_report=None,
                cursor_id=previous_cursor, config_hash=cfg_hash,
            )

        if previous_cursor is None:
            slots = sorted(
                {(f.subject_id, f.predicate) for f in self.store.iter_latest_facts()}
            )
        else:
            slots = list(self.store.iter_slots_touched_since(previous_cursor))

        exclude_ids, pending_sigs = self._prior_pending()
        report = run_transforms(
            self.store, self.config, self._now_ms(), run_id="dry-run", slots=slots,
            summarizer=self.summarizer, extra_exclude_ids=exclude_ids,
            pending_signatures=pending_sigs, dry_run=True,
        )
        stats["merges"] = len(report.merges)
        stats["staged"] = len(report.proposals)
        stats["demoted"] = len(report.demoted_ids)
        stats["dropped_proposals"] = report.dropped_proposals
        return RunReport(
            status="ok", run_id=None, below_threshold=False,
            threshold_stats=stats, transform_report=report,
            cursor_id=self._max_fact_id(), config_hash=cfg_hash,
        )

    # ── the run ──
    def run(
        self, *, on_batch: Optional[Callable[[], None]] = None, dry_run: bool = False
    ) -> RunReport:
        """Execute one maintenance run end to end (lease → thresholds → transforms).

        `on_batch` is a test hook invoked at every batch boundary (after the runner's
        own heartbeat) — used by the heartbeat/crash tests to observe cadence and to
        simulate a mid-run crash. Production passes None.

        `dry_run=True` delegates to `dry_run()` — report-only, zero writes, no lease.
        """
        if dry_run:
            return self.dry_run()

        cfg_hash = self.config.config_hash()
        claim_ms = self._now_ms()
        run_id, skipped_reason, took_over = self._try_claim_lease(claim_ms)
        if run_id is None:
            return RunReport(status="skipped", skipped_reason=skipped_reason, config_hash=cfg_hash)

        # We hold the lease. Everything below runs under it; we release it in finally.
        # A heartbeat fires at each PHASE boundary (here) and at every BATCH boundary
        # (via the batch hook installed below) — the §7.2 "every batch commit and at
        # least every 30 s" cadence. The propose phase is bounded by
        # proposal_budget_per_run, so it never runs long without a phase-boundary beat.
        def _beat() -> None:
            self.store.heartbeat_run(run_id, self._now_ms())
            if on_batch is not None:
                on_batch()

        cursor_id: Optional[str] = None
        stats: dict = {}
        report: Optional[TransformReport] = None
        below = False
        try:
            prior = self.store.last_finished_run(self.namespace)
            previous_cursor = prior.get("cursor_id") if prior else None
            last_finished_at = prior.get("finished_at") if prior else None

            over, threshold_stats = self._threshold_check(previous_cursor, last_finished_at)
            stats = dict(threshold_stats)

            if not over:
                below = True
                stats["below_threshold"] = True
                # A below-threshold run does NO work, so it must PRESERVE the cursor of
                # the last run that did (§6.6). Advancing it would strand an un-processed
                # duplicate on a quiet slot: the next real run scans
                # iter_slots_touched_since(previous_cursor) and still sees it. Carry the
                # previous ok-run's cursor forward unchanged (None on a first no-op).
                cursor_id = previous_cursor
                return self._finish_ok(run_id, stats, cursor_id, cfg_hash, took_over,
                                       below=True, report=None)

            # Advance-before-write: snapshot the cursor high-water NOW, before any
            # transform output is written (§6.6). Only a run that DOES work advances the
            # cursor — the run's own later outputs (summaries, maintenance episodes) get
            # higher ids and fall after this snapshot, excluded from the next delta.
            cursor_id = self._max_fact_id()

            # Over threshold: run the transform phase under a heartbeat cadence.
            _beat()  # heartbeat at the phase boundary

            # Slot-level transforms scan every slot that gained a member since the
            # PREVIOUS cursor (iter_slots_touched_since) — the cursor-gap fix (§6.6).
            if previous_cursor is None:
                # First-ever run: every slot is "new". Drive off all latest facts.
                slots = sorted({(f.subject_id, f.predicate) for f in self.store.iter_latest_facts()})
            else:
                slots = list(self.store.iter_slots_touched_since(previous_cursor))

            exclude_ids, pending_sigs = self._prior_pending()

            # The store batches (dedup_exact) fire on_batch/heartbeat at each boundary.
            self._install_batch_hook(_beat)
            try:
                report = run_transforms(
                    self.store,
                    self.config,
                    self._now_ms(),
                    run_id=run_id,
                    slots=slots,
                    summarizer=self.summarizer,
                    extra_exclude_ids=exclude_ids,
                    pending_signatures=pending_sigs,
                )
            finally:
                self._uninstall_batch_hook()

            _beat()  # final heartbeat before finishing
            stats["merges"] = len(report.merges)
            stats["staged"] = len(report.proposals)
            stats["demoted"] = len(report.demoted_ids)
            stats["dropped_proposals"] = report.dropped_proposals
            return self._finish_ok(run_id, stats, cursor_id, cfg_hash, took_over,
                                   below=False, report=report)
        except BaseException:
            # A failure DURING our run: mark our own row aborted (consistent DB per
            # per-batch commits) and re-raise. The next runner takes over from state.
            try:
                self.store.finish_run(run_id, "aborted", self._now_ms(), None, cursor_id)
            except Exception:
                pass
            raise

    def _finish_ok(
        self, run_id, stats, cursor_id, cfg_hash, took_over, *, below, report
    ) -> RunReport:
        self.store.finish_run(run_id, "ok", self._now_ms(), json.dumps(stats, sort_keys=True), cursor_id)
        return RunReport(
            status="ok",
            run_id=run_id,
            below_threshold=below,
            threshold_stats=stats,
            transform_report=report,
            cursor_id=cursor_id,
            config_hash=cfg_hash,
            took_over_run_id=took_over,
        )

    # ── batch-boundary heartbeat wiring ──
    def _install_batch_hook(self, beat: Callable[[], None]) -> None:
        """Wrap the store's batch() so each batch COMMIT fires a heartbeat (§7.2).

        The store exposes `batch()` as a context manager (Task 1). We wrap it so that
        on successful exit (a committed batch) we bump the heartbeat and fire on_batch.
        This is how heartbeat_at advances across batch boundaries during a long run and
        how the crash test observes a batch boundary. Restored in `_uninstall_batch_hook`.
        """
        store = self.store
        original = store.batch
        self._original_batch = original

        import contextlib

        @contextlib.contextmanager
        def _batch_with_beat():
            import time as _t

            t0 = _t.perf_counter()
            with original():
                yield
            # Committed successfully — record the batch duration and heartbeat.
            dt = _t.perf_counter() - t0
            if dt > self._max_observed_batch_s:
                self._max_observed_batch_s = dt
            beat()

        store.batch = _batch_with_beat  # type: ignore[method-assign]

    def _uninstall_batch_hook(self) -> None:
        original = getattr(self, "_original_batch", None)
        if original is not None:
            self.store.batch = original  # type: ignore[method-assign]
            self._original_batch = None
