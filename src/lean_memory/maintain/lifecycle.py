"""Proposal decide + apply — the maintenance review lifecycle (design spec §4.2-§4.4, §5).

DEVIATION (deliberate, approved): the spec's §9.3 file table does not name this
module — it folds "decide/apply" into `memory.py`. We host it HERE instead so
`memory.py` stays a thin, raw-SQL-free façade: the approve-and-apply transaction is
non-trivial (CAS → re-validation → per-kind verbs → applied_at, all in one
`store.batch()` window, with model/embedding work computed BEFORE the window per
§7.1), and belongs beside the transforms it replays, not inside the public API
object. `Memory.decide/promote` are one-line delegations to `decide()` here.

Contract (spec §5, §3.4):
  - Proposals are INVISIBLE until approved: staging wrote zero spine changes, so
    apply is the ONLY place a proposal touches the spine.
  - APPROVE-AND-APPLY is ONE transaction: CAS decide → RE-VALIDATE every referenced
    target (still is_latest=1; dedup pairs additionally still co-valid in the same
    slot) → replay the transform's verbs at apply-time t_a (the visibility theorem
    holds with t_a) → stamp applied_at.
  - Any STALE target ⇒ the whole proposal flips to status='expired',
    expiry_reason='stale_target' and the spine is left BYTE-IDENTICAL: the approve
    transaction is rolled back, and the expiry is what commits.
  - CAS 0-rows ⇒ already decided: an informative result, never an error, never a
    re-apply. A retry after a committed apply returns "already applied".
  - PROMOTION is explicit-only (§4.4): set_tier(fact, 'hot'). As a decision on an
    evict proposal it REJECTS the proposal and promotes; as a direct verb it is
    Memory.promote(fact_id). No automatic promotion anywhere.
  - TIMEOUT expiry is LAZY: decide() (and review_queue) expire a pending proposal
    whose expires_at < now instead of deciding it (status='expired',
    expiry_reason='timeout').

Offline & batch discipline (§7.1): the summary embedding (the only model/embedding
work in this module) is computed BEFORE the batch window; the lock-hold span holds
only row writes.
"""

from __future__ import annotations

import json
from typing import Optional

import numpy as np

from ..embed.base import Embedder, matryoshka_truncate
from ..extract.salience import score_salience
from ..store.base import Store
from ..types import Episode, Fact
from .transforms import _coalesced_last_access

# Decision verbs accepted by decide().
APPROVE = "approve"
REJECT = "reject"
EDIT = "edit"
PROMOTE = "promote"
_DECISIONS = frozenset((APPROVE, REJECT, EDIT, PROMOTE))


class _StaleTarget(Exception):
    """Internal signal: a re-validation failed inside the approve batch. Raising it
    rolls the batch back (batch() ROLLBACKs on exception) so the spine is untouched;
    the caller then commits the stale-target expiry in a fresh transaction."""


class _LostRace(Exception):
    """Internal signal: the CAS decide updated 0 rows (a concurrent writer already
    decided this proposal). Rolls the batch back; the caller reports already-decided."""


def decide(
    store: Store,
    embedder: Embedder,
    proposal_id: str,
    decision: str,
    *,
    now: int,
    decided_by: str,
    edited_text: Optional[str] = None,
    run_id: Optional[str] = None,
) -> dict:
    """Decide a proposal (approve | reject | edit | promote) and apply on approval (§5).

    Returns a JSON-friendly result dict; never raises for the ordinary "already
    decided" / "stale" / "timed out" outcomes (those are reported in the dict). Raises
    only for genuine caller errors: an unknown decision, or a dim-mismatched embedder
    on a summarize apply (a config error that must not silently corrupt the DB).
    """
    if decision not in _DECISIONS:
        raise ValueError(
            f"unknown decision {decision!r}; expected one of "
            f"{sorted(_DECISIONS)}"
        )

    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        return {"proposal_id": proposal_id, "outcome": "not_found"}

    # Lazy timeout expiry (§5): a pending proposal past expires_at is expired here
    # instead of decided. A non-pending proposal is never re-expired.
    if proposal["status"] == "pending" and proposal["expires_at"] < now:
        expired = store.expire_proposal(proposal_id, "timeout")
        if expired:
            return {
                "proposal_id": proposal_id,
                "outcome": "expired",
                "expiry_reason": "timeout",
            }
        # Lost the race to a concurrent decider — fall through to the status report.
        proposal = store.get_proposal(proposal_id) or proposal

    if proposal["status"] != "pending":
        return _already_decided(proposal)

    if decision == REJECT:
        return _reject(store, proposal, now=now, decided_by=decided_by)
    if decision == PROMOTE:
        return _promote_decision(store, proposal, now=now, decided_by=decided_by)
    # approve | edit
    return _approve(
        store, embedder, proposal,
        now=now, decided_by=decided_by,
        edited_text=edited_text if decision == EDIT else None,
        is_edit=(decision == EDIT),
        run_id=run_id,
    )


# ── promote as a direct verb (Memory.promote) ────────────────────────────────
def promote_fact(store: Store, fact_id: str, *, now: int) -> dict:
    """Explicit promotion of a single fact to the hot tier (§4.4) — the direct verb
    behind Memory.promote(). No proposal involved; set_tier is verb (c),
    predicate-invisible. A missing fact is reported, not raised."""
    fact = store.get_fact(fact_id)
    if fact is None:
        return {"fact_id": fact_id, "outcome": "not_found"}
    with store.batch():
        store.set_tier(fact_id, "hot")
    return {"fact_id": fact_id, "outcome": "promoted", "tier": "hot"}


# ── result helpers ────────────────────────────────────────────────────────────
def _already_decided(proposal: dict) -> dict:
    """Informative result for a proposal that is no longer pending (§5). Distinguishes
    an applied proposal ('already applied') from a merely-decided one — never an
    error, never a re-apply."""
    applied = proposal.get("applied_at") is not None
    return {
        "proposal_id": proposal["id"],
        "outcome": "already_applied" if applied else "already_decided",
        "status": proposal["status"],
        "applied_at": proposal.get("applied_at"),
    }


# ── reject (§3.4: zero spine trace) ──────────────────────────────────────────
def _reject(store: Store, proposal: dict, *, now: int, decided_by: str) -> dict:
    won = store.cas_decide_proposal(proposal["id"], "rejected", now, decided_by)
    if not won:
        return _already_decided(store.get_proposal(proposal["id"]) or proposal)
    return {"proposal_id": proposal["id"], "outcome": "rejected"}


# ── promote decision (on an evict proposal: reject + promote, §4.4) ───────────
def _promote_decision(store: Store, proposal: dict, *, now: int, decided_by: str) -> dict:
    """A PROMOTE decision on an evict proposal: REJECT the eviction and PROMOTE the
    referenced fact to hot (§4.4). One batch so the reject CAS and the set_tier
    co-commit. Re-validates that the fact is still latest (promoting a superseded row
    to hot is meaningless) — a stale target expires stale, spine untouched."""
    if proposal["kind"] != "evict":
        return {
            "proposal_id": proposal["id"],
            "outcome": "invalid_decision",
            "reason": f"promote is only valid on an evict proposal, not {proposal['kind']!r}",
        }
    payload = json.loads(proposal["payload_json"])
    fact_id = payload["fact_id"]
    try:
        with store.batch():
            won = store.cas_decide_proposal(proposal["id"], "rejected", now, decided_by)
            if not won:
                raise _LostRace()
            fact = store.get_fact(fact_id)
            if fact is None or not fact.is_latest:
                raise _StaleTarget()
            store.set_tier(fact_id, "hot")
    except _LostRace:
        return _already_decided(store.get_proposal(proposal["id"]) or proposal)
    except _StaleTarget:
        store.expire_proposal(proposal["id"], "stale_target")
        return {
            "proposal_id": proposal["id"],
            "outcome": "expired",
            "expiry_reason": "stale_target",
        }
    return {
        "proposal_id": proposal["id"],
        "outcome": "promoted",
        "fact_id": fact_id,
        "tier": "hot",
    }


# ── approve / edit (the approve-and-apply transaction, §5) ────────────────────
def _approve(
    store: Store,
    embedder: Embedder,
    proposal: dict,
    *,
    now: int,
    decided_by: str,
    edited_text: Optional[str],
    is_edit: bool,
    run_id: Optional[str],
) -> dict:
    kind = proposal["kind"]
    payload = json.loads(proposal["payload_json"])

    # `edit` = edited_text + approve semantics with human provenance. Only summarize
    # carries editable text; an edit on any other kind is a caller error.
    if is_edit and kind != "summarize":
        return {
            "proposal_id": proposal["id"],
            "outcome": "invalid_decision",
            "reason": f"edit is only valid on a summarize proposal, not {kind!r}",
        }

    # Status enum (§5): an edit-approve records status='edited' (human provenance);
    # a plain approve records status='approved'.
    decided_status = "edited" if is_edit else "approved"

    # ── Model/embedding work computed BEFORE the batch window (§7.1). Only
    # summarize embeds; the dim guard refuses a mismatched embedder up front. ──
    precomputed: dict = {}
    if kind == "summarize":
        summary_text = edited_text if is_edit else payload["summary_text"]
        precomputed["summary_text"] = summary_text
        precomputed["embedding"] = _embed_or_refuse(store, embedder, summary_text)
        precomputed["coarse"] = matryoshka_truncate(
            precomputed["embedding"], embedder.coarse_dim
        )

    try:
        with store.batch():
            won = store.cas_decide_proposal(
                proposal["id"], decided_status, now, decided_by,
                edited_text=edited_text,
            )
            if not won:
                raise _LostRace()
            if kind == "dedup_near":
                applied = _apply_dedup_near(store, payload, now=now)
            elif kind == "summarize":
                applied = _apply_summarize(
                    store, proposal, payload, precomputed,
                    now=now, is_edit=is_edit, run_id=run_id,
                )
            elif kind == "evict":
                applied = _apply_evict(store, payload)
            else:  # pragma: no cover — schema constrains kind to the three above
                raise ValueError(f"unknown proposal kind {kind!r}")
            store.mark_proposal_applied(proposal["id"], now)
    except _LostRace:
        return _already_decided(store.get_proposal(proposal["id"]) or proposal)
    except _StaleTarget:
        # The approve batch rolled back — status is back to 'pending' — so this CAS
        # to 'expired' wins and IS the write that commits. Spine untouched.
        store.expire_proposal(proposal["id"], "stale_target")
        return {
            "proposal_id": proposal["id"],
            "outcome": "expired",
            "expiry_reason": "stale_target",
        }

    return {
        "proposal_id": proposal["id"],
        "outcome": "applied",
        "kind": kind,
        "status": decided_status,
        "applied_at": now,
        **applied,
    }


def _embed_or_refuse(store: Store, embedder: Embedder, text: str) -> np.ndarray:
    """Embed the summary text with the apply-owner's embedder, refusing if its dim
    does not match the namespace's baked vec0 dims (§4.3 apply-ownership rule).

    The apply process OWNS the embedding; embedding with a mismatched embedder would
    corrupt the vec0 table (or fail deep in the insert). We check up front and raise a
    clear, actionable ValueError — the same class of guard as _check_existing_dims."""
    store_dim = getattr(store, "dim", None)
    store_coarse = getattr(store, "coarse_dim", None)
    if store_dim is not None and embedder.dim != store_dim:
        raise ValueError(
            f"cannot apply summarize: the apply embedder produces {embedder.dim}-dim "
            f"vectors but this namespace's vec0 table was baked at {store_dim} dims. "
            f"Apply with an embedder matching the namespace's dims, or the summary "
            f"vector would corrupt the index."
        )
    if store_coarse is not None and embedder.coarse_dim != store_coarse:
        raise ValueError(
            f"cannot apply summarize: the apply embedder's coarse dim "
            f"{embedder.coarse_dim} does not match this namespace's {store_coarse}."
        )
    return embedder.embed_one(text)


# ── per-kind apply mechanics (all inside the batch; §4.2-§4.4) ────────────────
def _apply_dedup_near(store: Store, payload: dict, *, now: int) -> dict:
    """DEDUP-NEAR approve → DEDUP-EXACT mechanics (§4.2): re-validate the pair is
    still co-valid in one slot, then retire the loser onto the survivor and merge
    usage stats per the §4.1 rule. Reuses transforms._coalesced_last_access — the
    merge formula is NOT re-derived here."""
    a_id, b_id = payload["fact_ids"]
    survivor_id = payload["proposed_survivor"]
    loser_id = b_id if survivor_id == a_id else a_id

    a = store.get_fact(a_id)
    b = store.get_fact(b_id)
    # Both must still be latest AND still co-valid in the same slot (both open, same
    # subject+predicate) — the §5 dedup re-validation.
    if a is None or b is None or not a.is_latest or not b.is_latest:
        raise _StaleTarget()
    if a.valid_to is not None or b.valid_to is not None:
        raise _StaleTarget()
    if (a.subject_id, a.predicate) != (b.subject_id, b.predicate):
        raise _StaleTarget()

    survivor = a if survivor_id == a_id else b
    loser = b if survivor_id == a_id else a
    # Merge usage stats over the pair (survivor + loser), the §4.1 rule: access_count
    # SUMMED, last_access = max coalesce(last_access, valid_at). Reuse the transforms'
    # coalesce helper so the recency-anchor rule has one definition.
    merged_access = survivor.access_count + loser.access_count
    merged_last_access = max(
        _coalesced_last_access(survivor), _coalesced_last_access(loser)
    )

    store.retire_duplicate(loser.id, survivor.id)
    store.merge_usage_stats(survivor.id, merged_access, merged_last_access)
    return {
        "survivor_id": survivor.id,
        "loser_id": loser.id,
        "merged_access_count": merged_access,
        "merged_last_access": merged_last_access,
    }


def _apply_summarize(
    store: Store,
    proposal: dict,
    payload: dict,
    precomputed: dict,
    *,
    now: int,
    is_edit: bool,
    run_id: Optional[str],
) -> dict:
    """SUMMARIZE approve — the §4.3 apply steps (embedding already precomputed):
      (1) re-validate every source still is_latest=1;
      (2) insert a maintenance EPISODE (source='maintenance', raw=report JSON);
      (3) insert the summary fact (predicate='summary', record_kind='summary',
          is_inference=1, valid_at=t_a — NEVER backdated — valid_to=NULL, tier='hot');
      (4) add_derivation(summary, source, run_id) rows;
      (5) set_tier(source, 'cold') for each source;
      (6) if a previous is_latest summary exists for the subject, supersede it at t_a.
    """
    source_ids = payload["source_fact_ids"]
    subject_id = payload["subject_id"]

    # (1) Re-validate: every source still latest, else the proposal expires stale.
    sources: list[Fact] = []
    for sid in source_ids:
        f = store.get_fact(sid)
        if f is None or not f.is_latest:
            raise _StaleTarget()
        sources.append(f)

    namespace = sources[0].namespace
    summary_text = precomputed["summary_text"]
    embedding = precomputed["embedding"]
    coarse = precomputed["coarse"]

    # (2) Maintenance episode — satisfies the fact.episode_id NOT NULL FK.
    report_raw = json.dumps(
        {
            "kind": "summarize",
            "proposal_id": proposal["id"],
            "run_id": proposal["run_id"],
            "source_fact_ids": list(source_ids),
            "applied_at": now,
            "edited": is_edit,
        },
        sort_keys=True,
    )
    episode = Episode(
        namespace=namespace, raw=report_raw, t_ref=now, source="maintenance"
    )
    store.add_episode(episode)

    # Salience: re-score via the salience module. An edit-approve carries human
    # provenance → source='user' (which the stub favors); a machine approve scores as
    # a non-user maintenance source, so a human-curated summary naturally outranks a
    # machine one with zero new ranking code (§4.3).
    salience = score_salience(
        summary_text,
        source="user" if is_edit else "maintenance",
        is_inference=True,
    )

    # (3) The summary fact. valid_at = now (t_a) — NEVER backdated, so it appears in
    # no past window (§4.3). is_inference=1, tier='hot', its own 'summary' slot.
    summary = Fact(
        namespace=namespace,
        subject_id=subject_id,
        predicate="summary",
        fact_text=summary_text,
        valid_at=now,
        valid_to=None,
        is_latest=1,
        episode_id=episode.id,
        salience=salience,
        is_inference=1,
        tier="hot",
        record_kind="summary",
        ingested_at=now,
        created_at=now,
    )
    store.add_fact(summary, embedding, coarse)

    # (4) Derivation lineage rows (summary ← each source).
    derive_run_id = run_id or proposal["run_id"]
    for sid in source_ids:
        store.add_derivation(summary.id, sid, derive_run_id, now)

    # (5) Demote each source to cold — they stay is_latest=1 and fully as-of visible;
    # they leave the default hot surface, where the summary now represents them.
    for sid in source_ids:
        store.set_tier(sid, "cold")

    # (6) Supersede a previous is_latest summary for the subject at t_a, if any. The
    # old summary is NOT a source, so the staleness cascade does not misfire on it.
    superseded_prev: Optional[str] = None
    prev = [
        f
        for f in store.find_latest_in_slot(subject_id, "summary")
        if f.id != summary.id
    ]
    if prev:
        old = min(prev, key=lambda f: f.id)  # deterministic; there is normally one
        store.supersede_fact(old.id, summary.id, valid_to=now)
        superseded_prev = old.id

    return {
        "summary_id": summary.id,
        "episode_id": episode.id,
        "source_ids": list(source_ids),
        "superseded_prev_summary_id": superseded_prev,
    }


def _apply_evict(store: Store, payload: dict) -> dict:
    """EVICT approve → set_tier(fact, 'cold') (§4.4). Re-validate the fact is still
    latest; a stale target expires stale."""
    fact_id = payload["fact_id"]
    fact = store.get_fact(fact_id)
    if fact is None or not fact.is_latest:
        raise _StaleTarget()
    store.set_tier(fact_id, "cold")
    return {"fact_id": fact_id, "tier": "cold"}
