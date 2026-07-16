"""MaintenanceConfig — the frozen knob set for the sleep-time maintenance job.

The engine has no config mechanism today; per the design spec (§3.6) this frozen
dataclass IS it. Every default here is spec-pinned (§3.6, §6.6) except
`evict_threshold`, which the spec leaves tunable — see its field comment.

Its canonical-JSON hash (`config_hash()`) is recorded per run in
`maintenance_run.config_hash`, matching the repo's frozen-config discipline: a
number produced under one config is traceable to the exact knobs that produced
it. Frozen (immutable, hashable) so a run's config can't drift mid-run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

# One epoch-ms day — ages in the transforms are computed in ms (the same unit as
# `now`, `valid_at`, `last_access`), so the day-valued knobs convert through this.
MS_PER_DAY = 1000 * 60 * 60 * 24


@dataclass(frozen=True)
class MaintenanceConfig:
    """Immutable configuration for a maintenance run (design spec §3.6, §6.6).

    Day-valued thresholds (`*_days`) are stored in days for human legibility and
    converted to ms by the transforms via `MS_PER_DAY`.
    """

    # ── DEDUP-NEAR (§4.2) ──
    #: Cosine floor on stored embeddings for a same-slot pair to be *proposed* as a
    #: near-duplicate. 0.95 per §3.6/§4.2 — below it the multivalued co-valid band
    #: ("likes jazz"/"likes blues" ~0.6-0.95) and near-but-distinct literals live.
    tau_near: float = 0.95

    # ── SUMMARIZE (§4.3) / EVICT (§4.4) age gate ──
    #: A fact must be older than this (in days) before SUMMARIZE or EVICT will touch
    #: it (§3.6). A hard guard in EVICT — never demote a fact younger than this.
    age_floor_days: int = 90
    #: A (subject, predicate) slot must hold at least this many latest facts before
    #: SUMMARIZE considers it a cluster worth consolidating (§3.6).
    min_cluster: int = 5

    # ── EVICT (§4.4) ──
    #: Standing value-score floor below which a still-latest fact is *proposed* for
    #: demotion. The spec (§3.6, §4.4) pins NO value here — it is explicitly left
    #: tunable. 0.15 is a conservative default: with salience alone contributing up
    #: to 0.5 to the score, only genuinely low-salience, stale, unaccessed facts fall
    #: below it. Documented as tunable per the spec.
    evict_threshold: float = 0.15
    #: The strict AUTO-band (§4.4): a fact matching ALL of (salience < this,
    #: access_count == 0, age > `auto_evict_age_days`) is demoted to 'cold' WITHOUT
    #: review. §3.6 pins the band as `salience<2 AND access_count=0 AND age>180d`.
    auto_evict_salience: float = 2.0
    auto_evict_age_days: int = 180

    # ── proposal queue (§3.6, §5) ──
    #: Proposals not decided within this many days expire (never auto-apply; silence
    #: ≠ consent, §0/§12). Sets each proposal's `expires_at = now + this` (§4.2).
    proposal_expiry_days: int = 30
    #: Max proposals a single run may stage. Beyond it the run truncates and REPORTS
    #: the drop (§8.1) — never a silent cap. Keeps the review queue small (§2.4).
    proposal_budget_per_run: int = 50

    # ── work thresholds (§6.6) — read by the runner (Task 5), housed here ──
    #: Below ALL of these, a run is a no-op. Facts since cursor ≥ this, OR cumulative
    #: salience of new facts ≥ `min_new_salience`, OR ≥ `max_days_between_runs` since
    #: the last run (generative-agents reflection trigger, rescaled — §6.6).
    min_new_facts: int = 200
    min_new_salience: float = 300.0
    max_days_between_runs: int = 7

    def config_hash(self) -> str:
        """SHA-256 of this config's canonical JSON (sorted keys, stable separators).

        Deterministic across processes/machines for identical field values — the
        traceability anchor stamped in `maintenance_run.config_hash`.
        """
        canonical = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
