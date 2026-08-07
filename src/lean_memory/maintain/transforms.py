"""The four maintenance transforms + the intra-run orchestrator (design spec §4).

Pure functions over the Store ABC. Two transforms AUTO-APPLY provably-safe verbs;
three PROPOSE judgment calls into the review queue with zero spine writes. The
orchestrator (`run_transforms`) enforces the load-bearing intra-run ordering
(§4.4): stage every proposal over the PRE-transform snapshot FIRST, then run the
auto transforms EXCLUDING any fact referenced by a staged proposal — so reviewer
evidence never drifts mid-run.

Offline & batch discipline (§7.1): all embedding reads and summarizer text are
computed BEFORE any `store.batch()` window — a batch holds only row writes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

from ..extract.contradiction import is_multivalued
from ..normalize import normalize_text
from ..store.base import Store
from ..types import Fact
from . import score
from .config import MS_PER_DAY, MaintenanceConfig
from .summarize import Summarizer

Slot = tuple[str, str]  # (subject_id, predicate)


# ── value-preserving text normalization (DEDUP-EXACT, §4.1) ──────────────────
# `normalize_text` is imported above, not defined here: it moved to
# `lean_memory.normalize` when WP15 made entity identity (`entity.name_key`) use
# the SAME fold. Two copies of a normalization is how the `_norm`/`normalize_text`
# drift started; this module re-exports the one definition so WP10a's callers and
# tests keep importing `maintain.transforms.normalize_text` unchanged.


# ── reports (what each transform did / would do) ─────────────────────────────
@dataclass
class Merge:
    """One exact-duplicate merge that DEDUP-EXACT performed."""

    slot: Slot
    survivor_id: str
    loser_ids: list[str]
    merged_access_count: int
    merged_last_access: Optional[int]


@dataclass
class StagedProposal:
    """A proposal DEDUP-NEAR / SUMMARIZE / EVICT staged into the review queue."""

    proposal_id: str
    kind: str
    fact_ids: list[str]  # every fact this proposal references (for exclusion + budget)


@dataclass
class TransformReport:
    """Aggregate outcome of a maintenance run's transform phase."""

    merges: list[Merge] = field(default_factory=list)
    demoted_ids: list[str] = field(default_factory=list)  # auto-band evictions
    proposals: list[StagedProposal] = field(default_factory=list)
    dropped_proposals: int = 0  # truncated by proposal_budget_per_run (never silent)

    @property
    def staged_fact_ids(self) -> set[str]:
        """Union of every fact referenced by a staged proposal — the auto-phase
        exclusion set (§4.4)."""
        out: set[str] = set()
        for p in self.proposals:
            out.update(p.fact_ids)
        return out


# ── helpers ──────────────────────────────────────────────────────────────────
def _coalesced_last_access(f: Fact) -> int:
    """The retriever's recency anchor for a fact: last_access or valid_at (§4.1/§4.4)."""
    return f.last_access if f.last_access else f.valid_at


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine over stored float32 vectors. Copies out of the read-only frombuffer
    view implicitly via numpy ops (no in-place mutation)."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _latest_nonsummary_in_slot(store: Store, slot: Slot) -> list[Fact]:
    """Co-valid is_latest=1 non-summary facts in a slot (DEDUP targets, §4.1/§4.2)."""
    subject_id, predicate = slot
    return [
        f
        for f in store.find_latest_in_slot(subject_id, predicate)
        if f.record_kind != "summary"
    ]


# ── DEDUP-EXACT (auto-apply, §4.1) ───────────────────────────────────────────
def dedup_exact(
    store: Store,
    config: MaintenanceConfig,
    now: int,
    slots: Iterable[Slot],
    *,
    exclude_ids: Optional[set[str]] = None,
    dry_run: bool = False,
) -> list[Merge]:
    """Auto-retire exact duplicates within each slot (§4.1).

    Target: co-valid is_latest=1 non-summary facts in one (subject, predicate) slot
    whose NORMALIZED fact_text is identical (value-preserving normalization only).
    Survivor = argmin(valid_at), tiebreak min id. Each loser is retired onto the
    survivor via `retire_duplicate` (verb (c) — the is_latest_only=False as-of
    surface is bit-identical); the survivor inherits the cluster's usage stats:
    access_count SUMMED, last_access = max over cluster of coalesce(last_access,
    valid_at) — the §4.1 rule that keeps the deduped fact's recency anchor so it is
    not de-ranked on the latest surface.

    `exclude_ids`: facts referenced by a staged proposal — never touched here (the
    intra-run ordering exclusion, §4.4). Each slot's mutations run in one batch().
    """
    exclude_ids = exclude_ids or set()
    merges: list[Merge] = []

    for slot in slots:
        facts = [f for f in _latest_nonsummary_in_slot(store, slot) if f.id not in exclude_ids]
        if len(facts) < 2:
            continue

        # Group by value-preserving normal form.
        clusters: dict[str, list[Fact]] = defaultdict(list)
        for f in facts:
            clusters[normalize_text(f.fact_text)].append(f)

        for norm, members in clusters.items():
            if len(members) < 2:
                continue  # a single fact is not a duplicate

            # Survivor = argmin(valid_at), tiebreak min id — preserves "since when".
            survivor = min(members, key=lambda f: (f.valid_at, f.id))
            losers = [f for f in members if f.id != survivor.id]

            # Merge usage stats over the WHOLE cluster (survivor included), computed
            # BEFORE the batch window (§7.1 — no logic-heavy reads inside the lock).
            merged_access = sum(f.access_count for f in members)
            merged_last_access = max(_coalesced_last_access(f) for f in members)

            # dry_run: compute the merge record but write NOTHING (no batch, no verbs).
            if not dry_run:
                with store.batch():
                    for loser in losers:
                        store.retire_duplicate(loser.id, survivor.id)
                    store.merge_usage_stats(survivor.id, merged_access, merged_last_access)

            merges.append(
                Merge(
                    slot=slot,
                    survivor_id=survivor.id,
                    loser_ids=[f.id for f in losers],
                    merged_access_count=merged_access,
                    merged_last_access=merged_last_access,
                )
            )

    return merges


# ── DEDUP-NEAR (propose only, §4.2) ──────────────────────────────────────────
def dedup_near(
    store: Store,
    config: MaintenanceConfig,
    now: int,
    slots: Iterable[Slot],
    *,
    run_id: str,
    budget: int = 1_000_000,
    skip_signatures: Optional[set[frozenset[str]]] = None,
    dry_run: bool = False,
) -> tuple[list[StagedProposal], int]:
    """Stage near-duplicate merge PROPOSALS — zero spine writes (§4.2).

    Target: same-slot co-valid pairs whose stored-embedding cosine >= tau_near and
    that are NOT textually identical (post-normalization — those are DEDUP-EXACT's
    job). Never auto-applied: the multivalued co-valid band and near-but-distinct
    literals make it a judgment call. Stages one proposal per qualifying pair with
    both fact ids/texts, cosine, slot, the multivalued flag, and the proposed
    survivor (argmin valid_at). evidence_backend='stored' (embeddings read back, not
    re-embedded). Respects `budget`; returns (staged, dropped_count).

    `skip_signatures`: `{frozenset(fact_ids)}` of dedup_near proposals ALREADY pending
    (from a prior run) — a pair matching one is not re-staged, so a crash-resumed run
    converges without double-staging identical evidence (§7.4 idempotence).
    """
    skip_signatures = skip_signatures or set()
    staged: list[StagedProposal] = []
    dropped = 0
    tau = config.tau_near
    expires_at = now + config.proposal_expiry_days * MS_PER_DAY

    for slot in slots:
        facts = _latest_nonsummary_in_slot(store, slot)
        if len(facts) < 2:
            continue
        # Read stored embeddings ONCE per fact (no re-embed, no batch window).
        embs: dict[str, np.ndarray] = {}
        for f in facts:
            v = store.get_embedding(f.id)
            if v is not None:
                embs[f.id] = v

        multivalued = is_multivalued(slot[1])
        # Deterministic pair order: by (valid_at, id) so proposals are reproducible.
        ordered = sorted(facts, key=lambda f: (f.valid_at, f.id))
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                if a.id not in embs or b.id not in embs:
                    continue
                if normalize_text(a.fact_text) == normalize_text(b.fact_text):
                    continue  # exact dup — DEDUP-EXACT owns it
                cos = _cosine(embs[a.id], embs[b.id])
                if cos < tau:
                    continue
                if frozenset((a.id, b.id)) in skip_signatures:
                    continue  # an identical pair is already pending — don't double-stage
                # Proposed survivor = argmin(valid_at) (a already sorts first).
                survivor = min((a, b), key=lambda f: (f.valid_at, f.id))
                payload = {
                    "slot": {"subject_id": slot[0], "predicate": slot[1]},
                    "fact_ids": [a.id, b.id],
                    "fact_texts": {a.id: a.fact_text, b.id: b.fact_text},
                    "cosine": round(cos, 6),
                    "multivalued": multivalued,
                    "proposed_survivor": survivor.id,
                    "evidence_backend": "stored",
                }
                if len(staged) >= budget:
                    dropped += 1
                    continue
                # dry_run: count the would-stage proposal, write NOTHING.
                pid = "dry-run" if dry_run else store.stage_proposal(
                    run_id=run_id,
                    namespace=a.namespace,
                    kind="dedup_near",
                    payload_json=json.dumps(payload, sort_keys=True),
                    created_at=now,
                    expires_at=expires_at,
                    evidence_backend="stored",
                )
                staged.append(
                    StagedProposal(proposal_id=pid, kind="dedup_near", fact_ids=[a.id, b.id])
                )

    return staged, dropped


# ── SUMMARIZE (propose only — STAGING side; apply is Task 6, §4.3) ───────────
def summarize(
    store: Store,
    config: MaintenanceConfig,
    now: int,
    summarizer: Summarizer,
    *,
    run_id: str,
    budget: int = 1_000_000,
    skip_signatures: Optional[set[frozenset[str]]] = None,
    dry_run: bool = False,
) -> tuple[list[StagedProposal], int]:
    """Stage SUMMARIZE proposals — zero spine writes, no embedding at stage time (§4.3).

    Per subject entity: gather latest non-summary facts older than age_floor_days in
    slots holding >= min_cluster such facts. Subjects are ranked by cluster HEAT —
    documented as the SUM of score.value() over the subject's qualifying facts (a
    query-free standing-value proxy; hotter subjects consolidate first). Payload
    carries the source fact ids + texts, the proposed summary text from `summarizer`,
    and evidence_backend = the summarizer's backend id. Respects `budget`; returns
    (staged, dropped_count).

    `skip_signatures`: `{frozenset(source_fact_ids)}` of SUMMARIZE proposals ALREADY
    pending (from a prior run) — a subject whose source set exactly matches one is not
    re-staged, so a crash-resumed run does not double-stage the same summary (§7.4).
    """
    skip_signatures = skip_signatures or set()
    staged: list[StagedProposal] = []
    dropped = 0
    age_floor_ms = config.age_floor_days * MS_PER_DAY
    expires_at = now + config.proposal_expiry_days * MS_PER_DAY

    # Group qualifying (old-enough) latest non-summary facts by subject, then slot.
    by_subject: dict[str, dict[Slot, list[Fact]]] = defaultdict(lambda: defaultdict(list))
    for f in store.iter_latest_facts():
        if f.record_kind == "summary":
            continue
        if (now - f.valid_at) < age_floor_ms:
            continue
        by_subject[f.subject_id][(f.subject_id, f.predicate)].append(f)

    # Rank subjects by cluster heat = sum of value() over their qualifying facts.
    def subject_heat(slots_map: dict[Slot, list[Fact]]) -> float:
        return sum(score.value(f, now) for fs in slots_map.values() for f in fs)

    ranked_subjects = sorted(
        by_subject.items(), key=lambda kv: (-subject_heat(kv[1]), kv[0])
    )

    for subject_id, slots_map in ranked_subjects:
        # Only slots meeting the cluster-size floor qualify (§4.3).
        qualifying = {s: fs for s, fs in slots_map.items() if len(fs) >= config.min_cluster}
        if not qualifying:
            continue
        # Sources = every qualifying fact for the subject, in a stable order.
        sources: list[Fact] = sorted(
            (f for fs in qualifying.values() for f in fs),
            key=lambda f: (f.valid_at, f.id),
        )
        if frozenset(f.id for f in sources) in skip_signatures:
            continue  # this exact source set is already pending — don't double-stage
        # Budget check BEFORE the summarizer call: a full budget must never invoke the
        # summarizer (once Ollama is the [llm] summarizer, that is a wasted generation).
        if len(staged) >= budget:
            dropped += 1
            continue
        # Summarizer text computed BEFORE any batch window (§7.1). Stage-time only —
        # no embedding here (embedding is the Task-6 apply path, §4.3).
        summary_text = summarizer.summarize(sources)
        payload = {
            "subject_id": subject_id,
            "source_fact_ids": [f.id for f in sources],
            "source_fact_texts": {f.id: f.fact_text for f in sources},
            "summary_text": summary_text,
            "evidence_backend": summarizer.backend_id,
        }
        # dry_run: count the would-stage proposal, write NOTHING.
        pid = "dry-run" if dry_run else store.stage_proposal(
            run_id=run_id,
            namespace=sources[0].namespace,
            kind="summarize",
            payload_json=json.dumps(payload, sort_keys=True),
            created_at=now,
            expires_at=expires_at,
            evidence_backend=summarizer.backend_id,
        )
        staged.append(
            StagedProposal(
                proposal_id=pid, kind="summarize", fact_ids=[f.id for f in sources]
            )
        )

    return staged, dropped


# ── EVICT (auto strict band + propose, §4.4) ─────────────────────────────────
def _evict_guarded(f: Fact, config: MaintenanceConfig, now: int) -> bool:
    """True iff `f` is a legal EVICT candidate — passes every §4.4 guard.

    Never propose/demote: salience >= 6, age < age_floor_days, record_kind='summary'.
    (Staged-proposal referencing is handled by the caller's exclusion set.)
    access_count==0 alone is NEVER sufficient (implicit — it is not a guard here; the
    value threshold and the auto-band decide).
    """
    if f.record_kind == "summary":
        return False
    if f.salience >= 6:
        return False
    age_ms = now - f.valid_at
    if age_ms < config.age_floor_days * MS_PER_DAY:
        return False
    return True


def evict_propose(
    store: Store,
    config: MaintenanceConfig,
    now: int,
    *,
    run_id: str,
    budget: int = 1_000_000,
    exclude_ids: Optional[set[str]] = None,
    dry_run: bool = False,
) -> tuple[list[StagedProposal], int]:
    """Stage EVICT (demotion) proposals for still-latest facts below the value floor
    but NOT in the strict auto-band — zero spine writes (§4.4).

    A guarded fact whose standing value() < evict_threshold and that is not eligible
    for the auto-band is proposed for demotion. Respects `budget`; returns
    (staged, dropped_count).
    """
    exclude_ids = exclude_ids or set()
    staged: list[StagedProposal] = []
    dropped = 0
    expires_at = now + config.proposal_expiry_days * MS_PER_DAY

    for f in store.iter_latest_facts():
        if f.id in exclude_ids:
            continue
        if not _evict_guarded(f, config, now):
            continue
        if _in_auto_band(f, config, now):
            continue  # the auto-band demotes it without review
        v = score.value(f, now)
        if v >= config.evict_threshold:
            continue
        payload = {
            "fact_id": f.id,
            "fact_text": f.fact_text,
            "value": round(v, 6),
            "salience": f.salience,
            "access_count": f.access_count,
            "evidence_backend": "score",
        }
        if len(staged) >= budget:
            dropped += 1
            continue
        # dry_run: count the would-stage proposal, write NOTHING.
        pid = "dry-run" if dry_run else store.stage_proposal(
            run_id=run_id,
            namespace=f.namespace,
            kind="evict",
            payload_json=json.dumps(payload, sort_keys=True),
            created_at=now,
            expires_at=expires_at,
            evidence_backend="score",
        )
        staged.append(StagedProposal(proposal_id=pid, kind="evict", fact_ids=[f.id]))

    return staged, dropped


def _in_auto_band(f: Fact, config: MaintenanceConfig, now: int) -> bool:
    """The strict auto-demote band (§4.4/§3.6): salience < auto_evict_salience AND
    access_count == 0 AND age > auto_evict_age_days. Guards are checked separately."""
    if f.salience >= config.auto_evict_salience:
        return False
    if f.access_count != 0:
        return False
    age_ms = now - f.valid_at
    return age_ms > config.auto_evict_age_days * MS_PER_DAY


def evict_auto(
    store: Store,
    config: MaintenanceConfig,
    now: int,
    *,
    exclude_ids: Optional[set[str]] = None,
    dry_run: bool = False,
) -> list[str]:
    """Auto-demote the strict-band facts to 'cold' without review (§4.4).

    Band (config): salience < auto_evict_salience AND access_count == 0 AND age >
    auto_evict_age_days, on a guarded, still-latest, non-excluded fact. Uses
    set_tier (verb (c), predicate-invisible). Returns the demoted fact ids.
    """
    exclude_ids = exclude_ids or set()
    demoted: list[str] = []
    for f in store.iter_latest_facts():
        if f.id in exclude_ids:
            continue
        if not _evict_guarded(f, config, now):
            continue
        if not _in_auto_band(f, config, now):
            continue
        if not dry_run:  # dry_run: count the would-demote, write NOTHING.
            store.set_tier(f.id, "cold")
        demoted.append(f.id)
    return demoted


# ── intra-run orchestrator (the load-bearing ordering, §4.4) ─────────────────
def run_transforms(
    store: Store,
    config: MaintenanceConfig,
    now: int,
    *,
    run_id: str,
    slots: Iterable[Slot],
    summarizer: Optional[Summarizer] = None,
    extra_exclude_ids: Optional[set[str]] = None,
    pending_signatures: Optional[dict[str, set[frozenset[str]]]] = None,
    dry_run: bool = False,
    auto_only: bool = False,
) -> TransformReport:
    """Run all four transforms in the spec-mandated intra-run order (§4.4).

    ORDER (load-bearing): stage ALL proposals (dedup_near, summarize, evict-propose)
    over the PRE-transform snapshot FIRST — so reviewer evidence never drifts — THEN
    run the auto transforms (dedup_exact, evict auto-band) EXCLUDING any fact
    referenced by a staged proposal. The global proposal budget
    (`proposal_budget_per_run`) is shared across the three propose transforms;
    truncation is REPORTED in `dropped_proposals`, never silent.

    `auto_only` (default False, preserving all existing behavior/tests): skip the
    Phase-1 propose transforms entirely and run ONLY the auto band (dedup_exact,
    evict auto-band). This is the `--auto-only` CLI switch (spec §6.1): apply the
    provably-safe transforms, stage NOTHING. With no staged proposals, the auto
    phase's exclusion set is just the prior-run pending ids (`extra_exclude_ids`).

    `slots` is materialized once (the pre-transform snapshot of touched slots) and
    reused for both the near-dup proposals and the exact-dup autos.

    Cross-run coupling (the runner supplies these; §4.4 'facts referenced by any
    staged proposal' + §7.4 crash idempotence):
      - `extra_exclude_ids`: fact ids referenced by PRIOR-run pending proposals —
        unioned into the Phase-2 auto exclusion so an auto transform never touches a
        fact a human is still reviewing.
      - `pending_signatures`: `{kind: {frozenset(fact_ids)}}` of prior-run pending
        proposals — a propose transform skips re-staging an identical-evidence
        proposal, so a crash-resumed run converges without duplicate proposals.
    """
    from .summarize import default_summarizer

    if summarizer is None:
        summarizer = default_summarizer()

    extra_exclude_ids = extra_exclude_ids or set()
    pending_signatures = pending_signatures or {}

    slots = list(slots)  # materialize once — reused across phases
    report = TransformReport()

    # ── Phase 1: STAGE ALL PROPOSALS over the pre-transform snapshot ──
    # Skipped entirely under `auto_only` (--auto-only): stage NOTHING, run only autos.
    if not auto_only:
        budget = config.proposal_budget_per_run
        remaining = budget

        near, dropped = dedup_near(
            store, config, now, slots, run_id=run_id, budget=remaining,
            skip_signatures=pending_signatures.get("dedup_near"), dry_run=dry_run,
        )
        report.proposals.extend(near)
        report.dropped_proposals += dropped
        remaining = budget - len(report.proposals)

        summ, dropped = summarize(
            store, config, now, summarizer, run_id=run_id, budget=max(0, remaining),
            skip_signatures=pending_signatures.get("summarize"), dry_run=dry_run,
        )
        report.proposals.extend(summ)
        report.dropped_proposals += dropped
        remaining = budget - len(report.proposals)

        # A fact referenced by a prior-run pending proposal is never re-proposed for
        # eviction (excluding its id covers both re-propose and auto-demote — §4.4).
        ev_prop, dropped = evict_propose(
            store, config, now, run_id=run_id, budget=max(0, remaining),
            exclude_ids=extra_exclude_ids, dry_run=dry_run,
        )
        report.proposals.extend(ev_prop)
        report.dropped_proposals += dropped

    # ── Phase 2: AUTO transforms, EXCLUDING every staged-proposal target ──
    # This run's staged ids UNION prior runs' still-pending referenced ids: neither a
    # fact a human is reviewing now nor one staged this run is auto-mutated.
    staged_ids = report.staged_fact_ids | extra_exclude_ids
    report.merges = dedup_exact(
        store, config, now, slots, exclude_ids=staged_ids, dry_run=dry_run
    )
    report.demoted_ids = evict_auto(
        store, config, now, exclude_ids=staged_ids, dry_run=dry_run
    )

    return report
