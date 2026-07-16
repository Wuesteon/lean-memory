# Sleep-Time Maintenance — Design

Date: 2026-07-16. Status: **approved design, rev 3** (rev 1 was revised
against a six-dimension adversarial verification pass; rev 3 folds in a
second, independent six-dimension round that confirmed one blocker — the
transitive duplicate-chain resurrection, fixed in §4.0/§10.2 — plus
packet-boundary and consistency fixes; see §14. The §12 decisions
were resolved by the user 2026-07-16 and WP10a/WP10b are registered in
`docs/superpowers/workpackets.md`; implementation plan:
`docs/superpowers/plans/2026-07-16-sleep-time-maintenance.md`). All `file:line` references verified against the working tree
at v0.1.3; empirical claims were tested with scratch scripts against the
project venv (sqlite-vec 0.1.9 / SQLite 3.53.2 / Python 3.13).

Companion docs: `docs/superpowers/specs/2026-07-08-strategic-direction-design.md`
(launch strategy — this work is **post-launch**, see §9),
`docs/superpowers/workpackets.md` (WP4 read-surface, WP5 deletion, WP6 TTL,
and the anti-goal this design partially amends — see §9.1).

## 0. Decision summary

An **offline "sleep-time" maintenance job** that runs between sessions and
cleans up stored memory — **deduplicating** entries, **summarizing** older
records into compact form, and **evicting** low-value ones — while preserving
the ADD-only spine and as-of query semantics, plus a **human review queue**:
the job auto-applies only provably-safe transforms overnight and stages
everything judgmental as *proposals* the user clicks through the next day, in
the web console **or conversationally through Claude Code via MCP tools**.

| Question | Decision | Why |
|---|---|---|
| Where does maintenance write? | Only through sanctioned verbs: append rows, `supersede_fact` (now with duplicate-cascade), `retire_duplicate` (is_latest flip + re-point of existing losers, §4.0), `set_tier` (hot↔cold) | Every verb is as-of-safe at the predicate level (§3.1, §4) |
| Physical deletion? | **None in v1.** No `DELETE` of fact, vec0, or FTS rows | Deleting index rows breaks as-of retrieval (§4.5); space reclaim is a v2 design |
| Ingest-path changes? | Two tiny hooks: duplicate-cascade closure + summary-staleness cascade | Verification proved offline-only transforms go temporally incoherent when later ingest contradicts them (§4.1, §4.3); both hooks are exact no-ops until maintenance has ever run |
| Who approves risky transforms? | Human, via a proposal queue (`pending → approved/rejected/edited/expired`) with apply-time target re-validation | LLM summaries and near-dup merges are judgment calls; no shipping memory product stages changes for approval — verified differentiator (§2.4, §14) |
| Where do proposals live? | New tables in the namespace `.db` (schema v2, user_version-gated migration) | Both MCP surfaces and the console must reach them; decide+apply must be one transaction |
| Unreviewed proposals? | **Expire** after N days (default 30). Never auto-apply | A memory write permanently changes agent behavior; silence ≠ consent |
| Which MCP server gets the tools? | The console stdio server (`observe_mcp.py`) + HTTP mount — what the plugin actually ships — plus core `mcp_server.py` for parity | There are **three** MCP surfaces in the tree; registering only in core would reach no plugin user (the v0.1.3 manifest-gap class, §6.3) |
| Config surface? | Frozen `MaintenanceConfig` dataclass; hash recorded per run in `maintenance_run.config_hash` | The engine has no config mechanism today; a frozen dataclass matches the repo's frozen-config discipline |
| Trigger? | CLI (`lean-memory-maintain`, dry-run by default) + cron recipe + MCP tools (also dry-run by default) + opt-in auto-spawn | The MCP stdio server has no idle/shutdown hook; external triggers are the honest ones (§6) |
| Launch impact? | Zero. Post-launch packet, default-off, no change to the first-run path | The quality gate just closed; nothing here touches it |

## 1. Map of the current implementation (what maintenance must respect)

### 1.1 The spine and its two sanctioned mutations

- **ADD-only means**: no row is ever deleted; the *only* retirement is
  `SqliteStore.supersede_fact` (`store/sqlite_store.py:168-176`), which
  updates exactly `superseded_by`, `valid_to`, `is_latest` on the old row and
  mirrors `is_latest=0` into `fact_vec`. Scoring metadata (`last_access`,
  `access_count`) is additionally mutable via `touch()`
  (`store/sqlite_store.py:309-314`).
- **As-of visibility predicate** (the invariant): a fact is visible at time
  `T` iff `valid_at <= T AND (valid_to IS NULL OR valid_to > T)` — applied
  identically in the dense arm (`_apply_as_of`, `store/sqlite_store.py:241-254`)
  and the sparse arm (`store/sqlite_store.py:290-293`). It reads **only**
  `valid_at`/`valid_to`. It never reads `is_latest`, `tier`, `expired_at`,
  `invalidated_by`, or `is_inference`. Note: `Memory.search` defaults
  `is_latest_only=True` *even when `as_of` is set* (`memory.py:213-230`) —
  the default-flag as-of query is a hybrid surface that ANDs both filters;
  the pure point-in-time surface is `is_latest_only=False`.
- **Ingest-time supersession**: `_apply_supersession` (`memory.py:154-173`)
  closes old facts at `valid_to = new.valid_at` (world-time), targeting only
  `find_latest_in_slot` rows (`is_latest=1`) — a fact that maintenance has
  already flipped to `is_latest=0` is **invisible to future ingest closure**.
  This is the root of the two interaction bugs verification found (§4.1,
  §4.3) and why the two ingest hooks exist.

### 1.2 Dormant seams the schema already reserved

- `fact.tier` `'hot'|'cold'` (`types.py:89`, `store/schema.py:57`) **and** a
  `tier` metadata column on the vec0 table (`store/schema.py:72`) — written at
  insert, never updated, never filtered. **Empirically verified** on the
  pinned sqlite-vec 0.1.9: vec0 TEXT-metadata UPDATE works, and KNN `MATCH`
  with `AND tier='hot' AND is_latest=1` filters correctly (§14).
- `expired_at` / `invalidated_by` audit columns (`store/schema.py:49-50`) —
  declared, round-tripped, read by no query path. Provenance seam.
- Scoring signals: `salience` [0,10] cached at write (`extract/salience.py:73`),
  `confidence`, `access_count`/`last_access` bumped on every top-k retrieval
  hit (`retrieve/retriever.py:113-114`), `is_inference`.
- Schema-version anchor: `PRAGMA user_version` stamped 1 at
  `store/sqlite_store.py:67-73`.

### 1.3 Operational constraints (verified, load-bearing)

1. **No `busy_timeout` anywhere in the engine.** `_connect`
   (`store/sqlite_store.py:51-61`) sets WAL + foreign_keys only. A second
   writer gets an *immediate* `database is locked`. The console compensates
   with its own `retry_busy` wrapper (3 attempts, exponential backoff,
   `console/.../engine.py:108-127`) — any engine-side timeout **stacks** with
   it (§7.1).
2. **Per-statement commits.** Every store mutation commits individually
   (`add_fact:166`, `supersede_fact:176`, `touch:314`). No unit-of-work
   exists; `batch()` (§4.0) is mandatory new plumbing. Caveat (verified):
   Python's `executescript()` implicitly commits — `batch()` must use
   `execute()` only.
3. **Three-surface sync.** `fact`, `fact_vec`, `fact_fts` must agree;
   `supersede_fact` is the pattern.
4. **Two independent clocks.** Ids sort by *ingestion* time (`new_id()`,
   `types.py:21-27`); `valid_at` is *world* time (episode `t_ref`). Backfills
   invert their order — no rule may assume id-order == valid_at-order.
5. **`touch()` top-k bias.** Only top-k hits bump access stats;
   `access_count=0` can mean "never cracked top-k", not "worthless". Also
   verified: `touch()` fires on **as_of searches too** — historical reads
   already mutate access stats today (§4.4).
6. **MCP stdio hygiene.** stdout is the JSON-RPC channel
   (`mcp_server.py:47-48`); the v0.1.3 banner bug is the cautionary tale.
7. **One SQLite file per namespace** (BET 4) — `find_latest_in_slot` /
   `supersede_fact` have no namespace in their WHERE clauses; correct only
   because of this. One store per file; never pool facts across stores.
8. **Console architecture** (`console/`, `ui/`): reads via read-only SQL
   (`inspect_sql.py`, schema-fingerprint tripwire at `inspect_sql.py:313`),
   writes **only** through `EngineGateway` (per-namespace asyncio lock +
   `retry_busy` + single worker thread, `engine.py:130-277`, which today
   exposes only `add`/`search`/`close`).
9. **No existing maintenance code.** The only deletion in the package is the
   whole-namespace file unlink in `memory_clear` (`mcp_server.py:137-147`).
10. **Three MCP surfaces exist** (verification finding, blocker-class):
    core `src/lean_memory/mcp_server.py` (stdio, `lean-memory-mcp`); the
    console's own stdio server `console/.../observe_mcp.py` (what
    `plugin/.mcp.json` actually ships via `lean-memory-console mcp`); and the
    console's HTTP mount `console/.../routes/mcp.py` (Docker mode). Tools
    registered only in core reach **no plugin user**.

## 2. Survey — how current systems maintain agent memory

Full citations in §13. Key mechanisms, grouped by the three transforms:

### 2.1 Consolidation / summarization

- **Letta/MemGPT sleep-time compute** (Lin et al. 2025, arXiv:2504.13171;
  Letta sleep-time agents): a *background agent* rewrites the primary agent's
  learned context between turns (`rethink_memory`), triggered every N=5
  primary steps on the message delta, tracked via a
  `last_processed_message_id` cursor. Amortization result (verified against
  the paper): ~5× less test-time compute at equal accuracy when many queries
  share a context. The canonical "offline pass" shape we adopt — but
  expressed as append-only derived facts, not in-place block rewrite.
- **Generative-agents reflection** (Park et al. 2023): trigger = cumulative
  importance of recent events > 150 (verified exact); transform = LLM
  synthesizes higher-level insights *appended to the same memory stream with
  provenance pointers to sources*. The closest published precedent for our
  summary facts + derivation links.
- **MemGPT recursive summarization** (Packer et al. 2023): two-threshold
  pressure (warn ~70%, flush ~100%); evicted messages *remain permanently in
  recall storage* — eviction from the working set is never deletion.
- **RAPTOR** (Sarthi et al. 2024): +20 abs. pts on QuALITY with GPT-4;
  builds summaries as *additional* tree nodes over intact leaves and
  retrieves across all layers including leaves — architecturally a
  summarize-and-retain design, not summarize-and-delete. (The paper does not
  ablate leaves-retained vs leaves-deleted; the architectural point stands,
  the causal attribution is ours.)
- **Event sourcing**: snapshots/"closing the books" bound replay cost while
  the log stays the immutable source of truth (Kurrent; Microsoft patterns;
  Kleppmann DDIA ch. 11-12: *derived data may be rebuilt; the source of truth
  is never rewritten*).

### 2.2 Deduplication / contradiction

- **Mem0** (Chhikara et al. 2025): inline LLM chooses ADD/UPDATE/DELETE/NOOP
  against top-10 similar memories — destructive by design; loses history. We
  reuse the *compare-against-neighbors-then-classify* step but resolve it as
  supersession-append.
- **Zep/Graphiti** (Rasmussen et al. 2025): bi-temporal edge invalidation —
  contradicted edges get `t_invalid` set, never deleted. The closest
  production kin of lean-memory's spine; LongMemEval +18.5% over
  full-context. Validates retirement-by-timestamp.
- **Letta archival memory is ADD-only with NO dedup** — cosine near-dup
  consolidation is an *open feature request* (letta-ai/letta #3116, Dec 2025;
  verified accurate): "merge duplicates preserving temporal metadata, as a
  sleep-time extension / scheduled background task." The leading production
  system has not solved exactly the thing this design specifies.
- **Kafka log compaction / LSM**: keep-latest-per-key with physical drops
  gated on "no live reader can still need it" (RocksDB drops tombstones only
  at the bottommost level with no live snapshot). v1 never physically drops;
  v2 reclaim must adopt this gating.

### 2.3 Eviction / forgetting

- **MemoryOS** (Kang et al. 2025): heat = α·visits + β·length + γ·exp(−Δt/μ),
  promotion threshold τ (verified exact); promote above τ, evict lowest-heat
  on overflow. We reuse the formula to *demote*, never delete.
- **MemoryBank** (Zhong et al. 2023): Ebbinghaus retention `e^(−Δt/S)`,
  strength grows on recall — decay modulates *ranking*, no deletion
  threshold. "Rank, don't delete."
- **FadeMem** (2026): importance-modulated decay with promote/demote
  *hysteresis* (θ=0.7/0.3); 45% storage reduction at comparable LoCoMo F1.
- **LUFY** (2025): aggressive forgetting (~90% of utterances) *improved*
  retrieval precision >17% — eviction is a precision feature, not a
  disk-space feature.
- **Rate-distortion view of memory compaction** (arXiv:2607.08032, 2026;
  verified real): frames compaction as a rate-distortion retain-vs-discard
  decision and shows query-agnostic keep-signals systematically discard
  query-relevant information — supporting a reversible, retrieval-backed
  regime. (Attribution note: the abstract does not state the specific
  "super-linear vs flat error" dichotomy; we cite the paper for the
  reversibility framing only.) Our maintenance stays in the reversible
  regime by construction: originals retained, demotions reversible,
  summaries re-derivable.

### 2.4 Human-in-the-loop review (the gap we fill)

- **No shipping memory product stages agent-proposed memory changes for
  approval** — Letta ADE, ChatGPT Manage Memory, Mem0 dashboard, Zep, LangMem
  are all *apply-then-curate*. This claim survived an active refutation
  attempt during verification (§14). The only approve-before-durable designs
  found: a 2026 hermes-agent proposal and generic LangGraph HITL interrupts.
- **Proven interaction patterns from adjacent domains**: Wikipedia pending
  changes (invisible-until-accepted, trust-gated auto-approval,
  batch-accept), GitHub suggested changes (propose-as-diff, single or batched
  commit), alert-fatigue research (confidence-based sampling, batch by
  entity, keep queues small or review quality collapses).
- **Security angle** (source pinned per verification): across recent
  evaluations, >90% of trials on frontier models (GPT-5-mini, Claude Sonnet
  4.5, Gemini 2.5 Flash) were vulnerable to memory poisoning through normal
  query-only interactions, even under strict safety constraints (MCFA
  arXiv:2603.15125; MINJA arXiv:2601.05504; Dash et al. arXiv:2606.04329).
  CHI 2025 user research shows demand for transparency/control over agent
  memory. A human gate on consolidation is a defense layer, not just UX.

*Human-memory analogy (analogy only): sleep consolidation distills episodic
traces into compact semantic memory without erasing the episodes — waking
writes stay fast and append-only; reorganization happens offline.*

## 3. Design principles

### 3.1 The visibility theorem (cornerstone, with verified scope)

At maintenance time `t_m`, if every action is one of:

- (a) append a fact with `valid_at >= t_m`,
- (b) close a fact with `valid_to = t_m` (or a later world-time closure
  event),
- (c) flip columns the as-of predicate never reads (`is_latest`, `tier`,
  `superseded_by`, `expired_at`, `invalidated_by`),

then for every `T < t_m` the **store-level as-of visibility predicate** is
bit-for-bit unchanged: closed facts still satisfy `valid_to > T`; appended
facts fail `valid_at <= T`; flipped columns are invisible to the predicate.

**Scope (per verification):**

1. *Predicate level, not retrieval top-k.* Both retrieval arms fetch a
   bounded candidate pool BEFORE as-of filtering (`dense_search` coarse KNN
   `LIMIT k*8`, `sparse_search` `k*2` FTS rows); an appended fact can
   displace pool candidates and thus perturb a past-T *top-k* result. This
   was empirically shown to be **pre-existing engine behavior under ordinary
   ingest** — maintenance verb (a) is identical in kind. The invariance
   guarantee, and the §10.1 test, are stated at the store predicate.
2. *Surface honesty.* Verb (c) flips ARE visible on the `is_latest` surfaces
   — default latest search AND the default-flag as-of query
   (`is_latest_only` defaults True even with `as_of`). This is consistent
   with how ordinary `supersede_fact` already behaves on those surfaces. The
   pure point-in-time surface (`is_latest_only=False`) is bit-identical.
3. *Ingest commutation (the fourth condition, added after verification).*
   Transforms must also **commute with future ingest**: any interval that
   ordinary ingest would have closed without maintenance must still get
   closed with it. Offline-only transforms fail this — retired duplicates
   and orphaned summaries are invisible to `find_latest_in_slot` and never
   get closed, producing *resurrection* and *stale-summary* wrong answers
   (empirically demonstrated, §14). The two ingest hooks (§4.1, §4.3) exist
   to restore commutation; both are exact no-ops on databases where
   maintenance has never run.

**Anti-rule** (from the first adversarial round): never close a row at
`valid_to = survivor.valid_at`. Ids sort by ingestion, `valid_at` by world
time; backfills make their order arbitrary → inverted intervals and coverage
gaps. Maintenance closure uses maintenance/world event times only.

### 3.2 Source of truth vs derived data

The `fact/episode/entity` spine is never rewritten; vec0 + FTS5 are derived
and *in v1 never deleted either* — as-of retrieval runs KNN/BM25 over them,
so deleting index rows silently removes past facts from as-of retrieval even
when the spine row survives. Space reclamation is a v2 design with LSM-style
reader gating (§9.2).

### 3.3 Two-tier autonomy

Auto-apply only transforms that are (i) as-of-safe per §3.1 *including
commutation* and (ii) information-preserving and reversible. Everything
judgmental becomes a **proposal**:

| Transform | Autonomy |
|---|---|
| Exact-duplicate retirement (identical normalized `fact_text`, same slot) | auto |
| Tier demotion of already-superseded old facts | auto (bookkeeping) |
| Near-duplicate merge (semantic band) | **propose** |
| Summarization (extractive or LLM) | **propose** |
| Eviction (demotion) of still-latest facts | **propose** (auto only below a strict config band) |

### 3.4 Proposals are invisible until approved

Staging writes zero spine changes. Approve = re-validate targets, then replay
the verbs at apply-time `t_a` (theorem holds with `t_a`). Reject = zero trace
on the spine. Expire = reject by timeout. The decision trail is itself
append-only data.

### 3.5 Offline-by-default discipline

Default summarizer = deterministic extractive stub (top-salience fact_texts;
honest, no model, labeled as such in the proposal). `[llm]` upgrades to
Ollama abstractive. The entire test suite stays offline; `LM_FORCE_STUBS` is
honored. Every proposal records the backend (`stub` / `ollama:<model>` /
embedder id) that produced its evidence, and the apply-time embedder id is
recorded alongside.

### 3.6 Configuration

`MaintenanceConfig` — a frozen dataclass (defaults: `tau_near=0.95`,
`age_floor_days=90`, `min_cluster=5`, `evict_threshold`, auto-band
`salience<2 AND access_count=0 AND age>180d`, `proposal_expiry_days=30`,
`proposal_budget_per_run=50`, work thresholds §6) — passed to
`Memory.maintain()` / the CLI. Its canonical-JSON hash is recorded per run in
`maintenance_run.config_hash`, matching the repo's frozen-config discipline.
No env-var soup, no config table in v1. (Verification found every "(config)"
in rev 1 was unbacked — the engine has no config mechanism; this is it.)

## 4. The transforms, precisely

### 4.0 New store verbs (the only new mutation surface)

```
Store ABC additions (store/base.py):
  iter_slots_touched_since(cursor_id) -> Iterator[(subject_id, predicate)]
      # slots that GAINED a member since the cursor: join new-facts-since-cursor
      # → DISTINCT slots; slot transforms then read the FULL slot via
      # find_latest_in_slot. (A bare id-cursor over facts misses duplicates
      # landing on long-quiet slots — verified gap.)
  iter_latest_facts(after_id=None) -> Iterator[Fact]        # id high-water scan (evict/summarize candidates)
  get_embedding(fact_id) -> np.ndarray | None               # read stored vector back (no re-embed; verified exact
                                                            #   float32 round-trip via np.frombuffer)
  retire_duplicate(loser_id, survivor_id)                   # is_latest=0 + superseded_by=survivor, valid_to UNTOUCHED,
                                                            #   + fact_vec.is_latest=0 (two-surface, one txn).
                                                            #   CHAIN INVARIANT: every OPEN retired duplicate
                                                            #   (superseded_by set, valid_to NULL) points DIRECTLY at
                                                            #   an is_latest=1 canonical survivor. Maintained two ways:
                                                            #   (i) resolve the survivor arg to its live canonical at
                                                            #   call time (depth 1); (ii) RE-POINT existing losers of
                                                            #   the loser: UPDATE fact SET superseded_by=:survivor
                                                            #   WHERE superseded_by=:loser AND valid_to IS NULL.
                                                            #   (ii) is load-bearing (rev-3 blocker, empirically
                                                            #   shown): without it, a chain B→A→D leaves B invisible
                                                            #   to the duplicate-cascade when D is later superseded —
                                                            #   B resurrects as a permanently-open interval on the
                                                            #   pure as-of surface. §10.2 pins the transitive case.
                                                            #   Re-pointing at maintenance time was chosen over a
                                                            #   recursive cascade (WITH RECURSIVE in supersede_fact)
                                                            #   to keep the hot ingest path single-level.
  set_tier(fact_id, tier)                                   # fact.tier + fact_vec.tier, one txn (vec0 TEXT-metadata
                                                            #   UPDATE verified working on sqlite-vec 0.1.9)
  batch() -> context manager                                # unit-of-work: BEGIN IMMEDIATE, suspends per-call commits,
                                                            #   one COMMIT at exit. MUST use execute() — executescript()
                                                            #   implicitly commits (verified). Model/embedding work is
                                                            #   FORBIDDEN inside the batch window (§7.1).
  + ledger/proposal CRUD (§5)

Modified verb:
  supersede_fact(old_id, new_id, valid_to) -> list[closed_id]
                                                            # + DUPLICATE-CASCADE (ingest hook 1): additionally
                                                            #   UPDATE fact SET valid_to=? WHERE superseded_by=old_id
                                                            #   AND valid_to IS NULL
                                                            #   — when a fact closes, its retired duplicates close at
                                                            #   the same world-time. A single level suffices BECAUSE
                                                            #   retire_duplicate's chain invariant guarantees every
                                                            #   open duplicate points directly at old_id. No-op until
                                                            #   retire_duplicate has ever produced such rows.
                                                            #   RETURNS the full closed set — [old_id] + cascade-closed
                                                            #   ids — so the summary-staleness cascade (§4.3) keys on
                                                            #   every closed row, not just the explicit target
                                                            #   (rev-3 seam fix).
```

### 4.1 DEDUP-EXACT (auto-apply)

*Target:* co-valid `is_latest=1` facts in one `(subject_id, predicate)` slot
with identical **normalized** `fact_text` — normalization is
**value-preserving only**: Unicode NFC, case-fold, whitespace collapse; never
stemming/synonyms (a lossy normalization could merge distinct multivalued
values — verified risk). These accumulate because an identical restatement is
classified ASSERTS and persisted again (`extract/contradiction.py:203`).

*Action:* survivor = `argmin(valid_at)` (tiebreak: min id) — preserves "since
when". For each loser: `retire_duplicate(loser, survivor)`; merge usage
stats: `survivor.access_count += loser.access_count`,
`survivor.last_access = max over cluster of coalesce(last_access, valid_at)`.
The last_access rule is deliberate (verified finding): the retriever's
recency anchor is `last_access or valid_at` (`retriever.py:97`) — keeping the
oldest-valid_at survivor without carrying the newest restatement's recency
would silently de-rank the deduped fact on the latest surface. §10 pins the
ranking delta.

*As-of argument:* `retire_duplicate` is verb (c) — the
`is_latest_only=False` as-of surface is bit-identical for all T (empirically
confirmed over a T grid). The `is_latest` surfaces — default search AND
default-flag as-of — see the collapse, exactly as they do for ordinary
supersession. `valid_to` stays NULL *at dedup time*; **ingest commutation**
is restored by the duplicate-cascade in `supersede_fact` (§4.0): when the
survivor is later closed at world-time V, its duplicates close at V too — all
of them, because the §4.0 chain invariant keeps every open duplicate pointing
directly at the live survivor — identical to the no-dedup counterfactual,
where functional-slot supersession
would have closed every co-valid restatement at V (`memory.py:168-173`).
Without the cascade, a retired duplicate resurrects as a permanently-open
interval after the survivor is superseded — an empirically demonstrated
wrong answer (§14).

*Notes:* the read-time `fact_text` dedup in `memory_search`
(`mcp_server.py:118-134`) is orthogonal — it collapses cross-slot repeats per
query and must stay. WP4 coordination: `history()` must distinguish
retirement-by-duplication (`superseded_by` set, `valid_to` NULL-until-cascade,
pointer aims at an *older* fact) from world-time supersession, e.g. by edge
kind, or its oldest→newest chain walk will mis-order merges.

### 4.2 DEDUP-NEAR (propose)

*Target:* same-slot co-valid pairs with stored-embedding cosine ≥
`tau_near` (0.95) that are *not* textually identical. Never auto-applied: the
multivalued co-valid band ("likes jazz"/"likes blues" sit at cosine 0.6-0.95
by design, `extract/contradiction.py:237`) and near-identical-but-distinct
literals ("salary 100k"/"110k") make this a judgment call.

*Proposal payload:* both fact_texts, cosine, slot, multivalued flag, proposed
survivor, evidence backend. *On approve:* apply-time re-validation (§5), then
DEDUP-EXACT mechanics. Multivalued slots require explicit reviewer
confirmation of co-reference ("same thing said twice, or two different
things?").

### 4.3 SUMMARIZE (propose)

*Target:* per subject entity, latest facts older than `age_floor` in slots
with ≥ `min_cluster` facts, ranked by cluster heat.

*Action on approve* — embedding and any model call computed **before** the
batch window (§7.1) — then in ONE `batch()` transaction:
1. Re-validate sources (§5): all still `is_latest=1`; else the proposal
   expires as stale.
2. Insert a **maintenance episode** (`source='maintenance'`, raw = run/report
   JSON) — satisfies the `fact.episode_id` NOT NULL FK
   (`store/schema.py:58`).
3. Insert the summary fact: **`predicate='summary'`** (its own slot per
   subject — never the source slot, so the functional-slot single-current-
   value invariant, `memory.py:154-165`, is untouched; verified),
   `record_kind='summary'`, `is_inference=1`, **`valid_at = t_a`** (never
   backdated → appears in no past window), `valid_to = NULL`, `tier='hot'`.
4. Insert `fact_derivation(summary_id, source_id, run_id)` lineage rows.
5. `set_tier(source, 'cold')` for each source — sources stay `is_latest=1`
   and fully as-of visible; they leave the default hot surface, where the
   summary now represents them.
6. If a previous summary exists for the subject:
   `supersede_fact(old_summary, new_summary, valid_to=t_a)`.

**Ingest hook 2 — summary-staleness cascade** (required; without it the
design ships a live contradiction, empirically demonstrated): when ingest
supersedes any fact, `_apply_supersession` additionally looks up
`fact_derivation WHERE source_id IN (closed ids)` (new index, §5) — *closed
ids* being the full set **returned by `supersede_fact`** (explicit target plus
duplicate-cascade-closed rows, §4.0), not merely the loop's own targets — and,
for
each derived summary still `is_latest=1`: sets `is_latest=0`,
`valid_to = new.valid_at`, `invalidated_by = new.id` (+vec mirror). The stale
summary leaves the default surface the moment its content stops being true;
the next maintenance run stages a fresh SUMMARIZE proposal for the subject.
As-of windows `[t_a, closure)` still show the summary — accurate: it was the
believed consolidated state during that period. No-op until
`fact_derivation` has rows.

*Summarizer seam:* `Summarizer` protocol; `ExtractiveStubSummarizer` default,
`OllamaSummarizer` behind `[llm]`. The reviewer can **edit** the text before
approving — recorded with human-curated provenance and re-scored with
`source='user'`, which the existing salience stub already favors — non-user
sources take a flat penalty (`extract/salience.py:119-120`) — so
human-curated memories naturally outrank machine summaries with zero new
ranking code.

*Apply ownership* (verified gap): the process executing apply owns the
embedding; it must embed with an embedder matching the namespace's baked vec0
dims (reuse the `_check_existing_dims` guard, `store/sqlite_store.py:75-104`)
or refuse to apply. Console applies run on `EngineGateway`'s single worker
thread; MCP applies use the server's Memory; CLI applies use its own.

### 4.4 EVICT (demotion; propose above a floor, auto below a strict band)

*Value score* (standing, query-free):

```
value = 0.5·(salience/10)
      + 0.3·exp(−DECAY_LAMBDA · (now − (last_access or valid_at)))   # VERBATIM the retriever's anchor (retriever.py:97)
      + 0.2·min(1, log1p(access_count)/log1p(10))
```

The recency anchor is deliberately the retriever's own (`last_access or
valid_at`, `retriever.py:97`) — rev 1 used `created_at`, which diverges on
backfills: a fact the retriever de-ranks as stale (old `valid_at`) would have
scored fresh under EVICT (recent ingest). "Stale" now means the same thing in
both places. (Rev 2 briefly wrote the anchor as `max(last_access or 0,
valid_at)`, which still diverged — on future-dated facts accessed before
their `valid_at`; rev 3 uses the retriever's expression verbatim.)

*Guards:* never propose facts with `salience ≥ 6`, age < `age_floor`,
`record_kind='summary'`, or facts referenced by any staged proposal.
`access_count=0` is never sufficient alone. *Auto band* (config, strict):
demote without review. Below `evict_threshold` otherwise → proposal.

*Intra-run ordering* (verified gap): stage ALL proposals over the
pre-transform snapshot first, THEN run auto-band transforms, excluding any
fact referenced by a staged proposal — so reviewer evidence never drifts
mid-run.

*Action:* `set_tier(fact, 'cold')`. *As-of:* verb (c), predicate-invisible.

*Reversibility (anti-ratchet), revised:* cold facts remain reachable via
`include_cold=True` and as-of search. **Promotion is explicit-only,
permanently** (decision 2026-07-16): review UI / `memory_review_decide`
promote verb / `Memory.promote()`. Rev 1's automatic promote-on-touch is
rejected, not deferred — verification showed `touch()` fires on as-of
searches too, so auto-promotion would make a read-only historical audit
query durably mutate the default hot surface. Reads never durably change
surfaces.

### 4.5 What deliberately does NOT happen in v1

- No `DELETE FROM fact_vec` / `fact_fts` — deleting index rows removes facts
  from as-of retrieval candidate pools even when the spine row survives.
- No episode compaction (provenance layer untouched); no `bench/` changes.
- No deletion of any kind — "delete" in the review UI routes to WP5's
  designed semantics when that lands.
- No ingest-path changes **except** the two cascades (§4.0, §4.3), each an
  exact no-op until maintenance has ever produced the rows they key on —
  the first-run path stays byte-identical.

## 5. Schema v2 (user_version-gated migration, 1→2)

```sql
-- Inside an `if user_version < 2:` migration branch (NOT the always-run
-- executescript(SCHEMA_SQL) blob): ALTER TABLE ADD COLUMN is not idempotent
-- and raises 'duplicate column name' on reopen (empirically verified).
ALTER TABLE fact ADD COLUMN record_kind TEXT NOT NULL DEFAULT 'fact';  -- 'fact'|'summary'

CREATE TABLE IF NOT EXISTS fact_derivation (
  summary_id TEXT NOT NULL REFERENCES fact(id),
  source_id  TEXT NOT NULL REFERENCES fact(id),
  run_id     TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (summary_id, source_id)
);
CREATE INDEX IF NOT EXISTS ix_derivation_source ON fact_derivation(source_id);
  -- the staleness cascade's lookup path (§4.3)

CREATE TABLE IF NOT EXISTS maintenance_run (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  started_at INTEGER NOT NULL, finished_at INTEGER,
  heartbeat_at INTEGER,
  trigger TEXT NOT NULL,                          -- 'cli'|'mcp'|'auto'|'console'
  cursor_id TEXT,
  config_hash TEXT, stats_json TEXT,
  status TEXT NOT NULL DEFAULT 'running'          -- 'running'|'ok'|'aborted'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_live
  ON maintenance_run(namespace) WHERE status='running';
  -- the INSERT is the atomic lease claim: a second runner gets a constraint
  -- error, not a silent second row (verified race gap in rev 1)

CREATE TABLE IF NOT EXISTS maintenance_proposal (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES maintenance_run(id),
  namespace TEXT NOT NULL,
  kind TEXT NOT NULL,                              -- 'dedup_near'|'summarize'|'evict'
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',          -- 'pending'|'approved'|'rejected'|'edited'|'expired'
  expiry_reason TEXT,                              -- 'timeout'|'stale_target'
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  decided_at INTEGER, decided_by TEXT,             -- 'console'|'mcp'|'expiry'
  applied_at INTEGER, edited_text TEXT,
  evidence_backend TEXT                            -- 'stub'|'ollama:<model>'|embedder id
);
```

Migration notes (revised per verification): `_init_schema`
(`store/sqlite_store.py:63-73`) must be restructured into a versioned
branch — v2 DDL runs only when `user_version < 2`, then stamps 2 (never
lowering a newer stamp). Ship the WP6 obligation: a checked-in v1-format
fixture DB + regression test (open, search, round-trip after upgrade). The
console fingerprint tripwire (`inspect_sql.py:313`) trips on the new CREATE
TABLE statements (verified: digest keys on lines containing `create` — the
bare ALTER alone would not trip it) and must be updated in the same change.

**Decide (CAS)** — verified race-safe under two concurrent writers:

```sql
UPDATE maintenance_proposal SET status=?, decided_at=?, decided_by=?, edited_text=?
 WHERE id=? AND status='pending';   -- 0 rows ⇒ already decided; report, don't re-apply.
                                    -- A retry after a committed apply returns
                                    -- 'already applied' (status/applied_at), not an error.
```

**Approve-and-apply**, one `batch()` transaction: CAS decide → **re-validate
targets** (every referenced fact still `is_latest=1` and, for dedup, still
co-valid in-slot; namespace file still present) → apply verbs → stamp
`applied_at`. Any stale target ⇒ the whole proposal flips to
`status='expired', expiry_reason='stale_target'` and the spine is untouched.
CAS on the proposal row alone is *not* sufficient after up to 30 days of live
ingest (verified gap).

## 6. Triggers

### 6.1 CLI (primary)

`lean-memory-maintain --root $LM_DATA_ROOT [--namespace NS] [--apply]
[--auto-only] [--json]` — one name, consistent everywhere (rev 1 mixed a
subcommand form; verified inconsistency). Lives in the **core** package
(`[project.scripts]`, beside `lean-memory-mcp`): cross-process safety comes
from the lease + busy_timeout + `batch()` (§7), not from the console's
in-process gateway, so no core→console dependency inversion. **Dry-run is
the default**; `--apply` executes the auto band and stages proposals.

### 6.2 Scheduler recipe (docs)

cron/launchd/systemd-timer invoking the CLI off-hours.

### 6.3 MCP tools

Registered on the surfaces users actually get (§1.3.10): the console stdio
server `observe_mcp.py` (what the plugin ships) and the HTTP mount
`routes/mcp.py`, with core `mcp_server.py` gaining the same four for
non-plugin stdio users. `server.json` and `plugin/.mcp.json` reconciled in
the same change (the v0.1.3 manifest lesson).

- `memory_maintenance_run(namespace, apply=False)` — **dry-run by
  default, symmetric with the CLI** (rev 1's asymmetry let an agent
  mutate state where the CLI would not); returns stats + would-stage /
  staged counts.
- `memory_maintenance_status(namespace)` — ledger only; guaranteed not to
  force the lazy model build (reads the DB, never constructs backends).
- `memory_review_queue(namespace, kind=None, limit=20)` — pending
  proposals with evidence, grouped by entity.
- `memory_review_decide(namespace, proposal_id, decision, edited_text=None)`
  — approve | reject | edit | promote.

### 6.4 Review workflow entry point

An MCP prompt `review-memory-maintenance` (`@mcp.prompt()` — verified
supported by the shipped mcp 1.28.0) on the console stdio server, **plus** a
plugin command file (`plugin/commands/review-memory.md`) — the mechanism the
plugin already uses — since MCP-prompt surfacing is a client capability we
cannot verify from this repo. The workflow: fetch queue → present batched by
entity/kind with evidence → collect verdicts → decide per item → summarize.
The prompt text **forbids the client agent from deciding without an explicit
user verdict** — agent-mediated review must remain human review. Batch verbs
map only to explicit user statements ("approve all exact dedups").

### 6.5 Opt-in auto-spawn

(`LM_MAINT_AUTO=1`, default OFF): on **first tool call** (inside `_mem()` —
the lazy build means there is no "server start" hook; rev 1 misstated this),
a single indexed `maintenance_run` read decides staleness; if stale, spawn
`lean-memory-maintain --apply --auto-only` with exactly:
`Popen(stdin=DEVNULL, stdout=DEVNULL, stderr=<log file or DEVNULL>,
start_new_session=True, close_fds=True)` — fd 1 must never be inherited
(the v0.1.3 stdout bug class; §10 extends the stdout-hygiene test to
cover this). The child outlives or dies with the server safely
(own session; no zombies — the parent never waits, `start_new_session`
reparents on parent exit).

### 6.6 Work thresholds & cursor semantics

*Work thresholds* (from `MaintenanceConfig`; below them the run is a no-op):
facts since cursor ≥ 200, OR cumulative salience of new facts ≥ 300
(generative-agents reflection trigger, rescaled), OR ≥ 7 days since last run.

*Cursor semantics* (split per verification): `iter_latest_facts` uses the id
high-water mark; **slot-level transforms re-scan every slot that gained a
member since the cursor** (`iter_slots_touched_since`) — a duplicate landing
on a long-quiet slot is otherwise invisible. The cursor advances before any
transform output is written, and maintenance-created rows
(`record_kind='summary'`, maintenance episodes) are excluded from candidate
scans, so the job's own outputs never re-enter its candidate set.

## 7. Concurrency & crash safety

### 7.1 Lock budget (revised — rev 1's numbers didn't survive verification)

- Engine-wide `PRAGMA busy_timeout` = **1500 ms** on every `_connect`.
  Rationale: the console's shipped `retry_busy` wrapper retries 3× *around*
  engine calls — an engine timeout of 5000 ms would stack to ~15 s worst-case
  per gateway `add()`; 1500 ms bounds the stack at ≈4.6 s and a single MCP
  `memory_add` stall at 1.5 s. Maintenance-opened connections use 5000 ms.
  In-process triggers (`Memory.maintain()`, the MCP tools) reach 5000 by
  opening a **dedicated maintenance `SqliteStore`** on the namespace file for
  the run's duration and closing it after — never by re-tuning the serving
  store's connection, whose budget stays untouched (rev-3 wiring fix; the
  CLI's own process does the same).
- **No model work inside a batch window.** Embeddings and summaries are
  computed before `BEGIN IMMEDIATE`; the lock-hold window contains only row
  writes. Empirically verified: a writer holding the lock past another
  connection's busy_timeout surfaces `database is locked`, not a clean
  retry — long batches would fail the live server's writes.

### 7.2 Lease (revised — atomic claim)

The partial unique index `ux_run_live` (§5) makes the `maintenance_run`
INSERT the atomic lease claim: BEGIN IMMEDIATE → check live-heartbeat row →
INSERT (loser hits the constraint) → COMMIT. Heartbeat: `heartbeat_at`
updated at every batch commit and at least every 30 s; stale threshold =
`max(5 min, 10× longest observed single-batch duration)`. A run whose
heartbeat is stale may be marked `'aborted'` and its namespace taken over;
takeover never rolls anything back (idempotent transforms + consistent
per-batch commits make partial runs safe).

### 7.3 `memory_clear` vs a live maintenance run (honest statement)

POSIX unlink defeats existence-checking: the maintenance job's open handle
keeps committing to the unlinked inode, and those commits are silently lost
(rev 1's per-batch existence check only reduces wasted work — verified
BROKEN as a safety claim). v1 fix: `memory_clear` refuses (returns an
explanatory error) while a live-heartbeat maintenance lease exists for the
namespace; the maintenance job likewise skips namespaces cleared mid-run at
its next batch boundary. Residual sliver (clear lands between lease-check
and unlink) is documented as a known limitation; full cross-process file
locking is deliberately out of scope for v1.

### 7.4 Crash safety

Short `batch()` transactions co-commit every mutation with its
derivation/ledger rows. A crash leaves a consistent DB and a
`status='running'` row with a stale heartbeat; the next run marks it
`'aborted'` and proceeds. Idempotence = resumability: transforms are pure
functions of DB state (already-deduped slots yield nothing; covered sources
are excluded via `fact_derivation`; a re-tried apply hits CAS 0-rows and
reports "already applied").

## 8. Retrieval changes (small, gated)

- `dense_search`/`sparse_search` gain a tier filter — dense via
  `AND tier='hot'` on the vec0 metadata column (**empirically verified** on
  sqlite-vec 0.1.9), sparse in the existing per-row recheck (single source of
  truth: both arms read the flag that `set_tier` writes to both surfaces in
  one txn; §10 pins arm agreement). Applied **only** in default latest-mode
  searches: **`as_of` queries never filter tier**, and `include_cold=True`
  opts out explicitly.
- Every existing row has `tier='hot'` ⇒ byte-identical behavior for anyone
  who never runs maintenance. First-run path untouched.
- `Memory` gains `maintain()`, `review_queue()`, `decide()`, `promote()`,
  and `search(..., include_cold=False)`.
- **`EngineGateway` gains four public methods** (`maintain`, `review_queue`,
  `decide`, `promote`) — real plumbing, not a façade freebie (verified: the
  gateway exposes only `add/search/close` today). Each wraps `retry_busy` +
  the per-namespace asyncio lock + the single worker thread. Documented
  consequence: a console-invoked `maintain()` holds that namespace's gateway
  lock, so console-origin maintenance runs in short chunks (per-batch lock
  release) to avoid starving live add/search.

### 8.1 Console review surface

New `Review` page (`ui/src/pages/Review.tsx` + nav entry): pending proposals
grouped by entity, suggested-changes-style before/after view, verbs
approve / keep (reject) / edit-then-approve / promote; batch approve per
group. Reads beside `routes/views.py` via `inspect_sql` additions (paginated
envelope pattern); decisions POST through the new `EngineGateway` methods.
Review-fatigue levers: batch-by-entity, an evidence-confidence sort
(highest-confidence first, so batch-approve stays safe to reach for), and the
`proposal_budget_per_run` cap (default 50). No lever auto-applies anything —
the spine changes only on an explicit verdict (§3.3, §3.4, §12); rev 3
removed a stray "auto-approve confidence band" phrase here that contradicted
that invariant.

## 9. Rollout, packaging, effort

### 9.1 Roadmap fit — the anti-goal must be amended, not ignored

`workpackets.md` listed "Consolidation/summarization passes" as a decided
anti-goal — rationale: "*managed-service concerns; the embedded positioning
sidesteps them*". A local, embedded, default-off sleep-time job does not
contradict that rationale; a hosted consolidation service still would.
**Decided 2026-07-16 (user):** the anti-goal is amended to "hosted/managed
consolidation services" and **WP10a/WP10b are registered** in
`workpackets.md` with this spec as their design doc. Sequencing: strictly
**post-launch** (blocked by WP1), a conscious strategy addition recorded in
the packet table — not gated on the six-week read.

### 9.2 Phasing

- **v1 (WP10a, ~1.5 weeks — revised up for the ingest hooks + migration
  restructuring):** store verbs + `batch()` + busy_timeout + duplicate-
  cascade + staleness cascade; versioned migration + fixture test + the
  console `EXPECTED_SCHEMA_FINGERPRINT` update (§5 — the v2 DDL trips it,
  so it must land with the migration);
  DEDUP-EXACT auto (decision 2026-07-16: auto from day one); EVICT auto-band
  + proposals; DEDUP-NEAR proposals; SUMMARIZE proposals (extractive stub);
  CLI + ledger/lease; `MaintenanceConfig`; MCP tools on all three surfaces +
  prompt + plugin command; as-of grid test at the store predicate. Review
  via MCP is fully usable without UI.
- **v1.1 (WP10b, ~3-4 days — decision 2026-07-16: starts right after WP10a
  merges, not gated on the demand read):** console Review page +
  `EngineGateway` methods + `inspect_sql` proposal reads. (The fingerprint
  update is **v1** work, per §5 — deferring it would merge WP10a with a red
  console suite; rev 3 fixed this doc's earlier triple-assignment.)
- **v2 (design-first):** space reclamation (drop full-dim vectors for cold
  facts, keep coarse 256-dim; LSM-style reader gating; explicit
  `--reclaim`), Ollama summarizer default-on with `[llm]`, WP5-integrated
  deletion verbs in the review UI. (Automatic promote-on-access is decided
  against permanently, §4.4 — not a v2 item.)

### 9.3 Files touched (v1)

| File | Change |
|---|---|
| `store/base.py`, `store/sqlite_store.py` | new verbs, `batch()`, busy_timeout, duplicate-cascade in `supersede_fact`, tier filters, ledger/proposal CRUD, versioned `_init_schema` |
| `store/schema.py` | schema v2 DDL (§5) |
| `memory.py` | staleness cascade in `_apply_supersession`; `maintain/review_queue/decide/promote` façade; `include_cold` |
| `maintain/{__init__,transforms,score,summarize,runner,config,cli}.py` | NEW package |
| `mcp_server.py` | 4 tools + opt-in auto-spawn (Popen primitives per §6.5) |
| `console/.../observe_mcp.py`, `console/.../routes/mcp.py` | same 4 tools + prompt (the surfaces the plugin ships) |
| `console/.../inspect_sql.py` | `EXPECTED_SCHEMA_FINGERPRINT` update — the §5 DDL trips it; same change as the migration |
| `console/.../engine.py` (v1.1) | 4 new `EngineGateway` methods |
| `plugin/` (`.mcp.json`, `commands/review-memory.md`), `server.json` | manifest reconciliation + command file |
| `pyproject.toml` | `lean-memory-maintain` script |
| `tests/test_maintenance_*.py` | §10 |
| `console/`, `ui/` (v1.1) | review router + `inspect_sql` proposal reads + Review page |

## 10. Test plan (the invariance argument, executable)

1. **As-of grid invariance, at the store predicate** (headline): corpus with
   backfills, functional + multivalued slots, supersessions; snapshot the
   ids satisfying the visibility predicate (direct store query,
   `is_latest_only=False`) over a T grid; run every transform; assert
   identical sets for all `T < t_m`, intended deltas for `T ≥ t_m`.
   Explicitly documented as a predicate-level guarantee — retrieval top-k
   has bounded-pool perturbation identical in kind to ordinary ingest
   (verified pre-existing). The propose-transforms' legs of this grid
   necessarily run against the proposal APPLY path — they have no spine
   effect before approval.
2. **Resurrection** (the rev-1 killer): dedup a slot, then supersede the
   survivor via ordinary ingest; assert the cascade closed the loser at the
   same world-time and `as_of` after the supersession returns only the new
   fact. **Transitive variant (the rev-3 killer):** retire B→A, then retire
   A→D (the DEDUP-NEAR apply path), then supersede D via ordinary ingest;
   assert the second retirement re-pointed B to D and the cascade closed
   BOTH A and B at the same world-time. Plus a standing invariant check run
   after every maintenance/apply test: zero rows with `superseded_by` set,
   `valid_to` NULL, whose `superseded_by` target is not `is_latest=1`.
3. **Stale summary** (the other rev-1 killer): summarize a slot, then
   contradict a source via ordinary ingest; assert the summary left the
   default surface (`is_latest=0`, `valid_to=new.valid_at`,
   `invalidated_by` set) and the next run stages a fresh proposal.
4. Survivor rule argmin(valid_at); no inverted intervals
   (`valid_to > valid_at` asserted post-run); normalization is
   value-preserving (case/whitespace only).
5. Multivalued guard: "likes jazz"+"likes blues" never auto-merged; near-dup
   only proposes.
6. Tier: two-surface sync; dense/sparse arm agreement; `as_of` ignores tier;
   byte-identical default when nothing is cold; `as_of × include_cold ×
   tier` matrix.
7. Proposal lifecycle: CAS double-decide across two frontends; re-apply after
   commit returns "already applied", not an error; timeout expiry;
   **stale-target expiry** (target superseded between stage and approve →
   apply rejects, spine byte-identical by full-DB hash); edited-approve
   records human provenance and re-scores with `source='user'`; reject
   leaves the spine byte-identical.
8. Ranking honesty: latest-mode top-k delta after DEDUP-EXACT is pinned
   (last_access merge rule keeps the deduped fact's recency anchor).
9. Crash/resume: kill between batches; DB consistent; lease takeover; re-run
   converges; no double-summary; no orphaned summary. Zero-eligible-work run
   is a clean no-op (threshold gate).
10. Ingest hooks are no-ops pre-maintenance: full ingest+search byte-
    equivalence on a DB where maintenance never ran (first-run pin).
11. Offline: whole suite on FakeEmbedder/StubTyper; stdout-hygiene test
    extended to the maintenance tools and the auto-spawn child (fd 1 never
    inherited).
12. Migration: v1-format fixture DB opens, upgrades once, reopens cleanly
    (the ALTER-idempotence trap), round-trips.
13. Existing pins stay green: `test_spine.py`, `test_asof_sparse.py`,
    `test_functional_slot_supersession.py`, `test_search_now.py`.

## 11. Comparison to current science — adopted / rejected / novel

**Adopted:** offline pass decoupled from the write path (Letta sleep-time,
LightMem, LangMem background); reflection-style threshold trigger and
append-with-provenance summaries (generative agents); demotion-not-deletion
paging (MemGPT recall storage; MemoryOS heat as a demotion signal);
summaries-over-intact-leaves (RAPTOR); retirement-by-timestamp (Zep/Graphiti
bi-temporal invalidation); compare-against-neighbors-then-classify (Mem0's
step, our verbs); derived-vs-source split and reader-gated future reclaim
(Kleppmann; Kafka/LSM; Datomic/XTDB logical-vs-physical).

**Rejected, deliberately:** inline destructive UPDATE/DELETE (Mem0, LangMem);
in-place neighbor rewriting (A-MEM evolution); hard forgetting thresholds
(MemoryOS eviction, LUFY deletion) — we take the precision upside as tier
demotion; auto-apply-on-expiry (Wikipedia timed flagged revisions) — silence
must mean expire, not consent.

**Novel (not found shipping anywhere):** (1) maintenance with an as-of
preservation argument reduced to three verbs **plus an ingest-commutation
condition**, pinned by executable predicate-level tests; (2) a staged
human-review queue for memory maintenance with dual frontends (web console +
conversational MCP review in Claude Code) — every surveyed product is
apply-then-curate; (3) the reversibility stance made concrete: all compaction
reversible, originals retained, summaries re-derivable and
staleness-invalidated by live ingest.

## 12. Decisions (resolved with the user, 2026-07-16)

1. **Roadmap:** anti-goal amended to hosted-only; WP10a/WP10b registered in
   `workpackets.md`, blocked by WP1, not gated on the six-week read. (§9.1)
2. **Proposal expiry (default taken, not user-asked):** `expires_at` = 30
   days. Expired *summarize* proposals re-stage naturally on a later run —
   the candidate scan re-discovers any still-qualifying cluster that has no
   live summary; expired *dedup/evict* proposals likewise re-stage if their
   evidence still holds. Nothing stays dead by fiat; nothing auto-applies.
3. **DEDUP-EXACT: auto from day one.** It is the one transform verified
   as-of-safe in isolation AND under later ingest (with the cascade), and
   review attention belongs on the judgment calls. Both CLI and MCP paths
   stay dry-run by default regardless.
4. **Promotion: explicit-only, permanently.** Reads never durably change
   surfaces. (§4.4)
5. **WP10b starts right after WP10a merges** — the dual-frontend review
   story is core to the feature, not demand-gated.

## 13. Sources (primary)

- MemGPT: Packer et al., arXiv:2310.08560 · Sleep-time Compute: Lin et al.,
  arXiv:2504.13171 · Letta sleep-time agents docs · Letta issue #3116
- Generative Agents: Park et al., arXiv:2304.03442 · Reflexion:
  arXiv:2303.11366 · ExpeL: arXiv:2308.10144 · Voyager: arXiv:2305.16291
- Mem0: arXiv:2504.19413 · Zep/Graphiti: arXiv:2501.13956 · LangMem docs ·
  A-MEM: arXiv:2502.12110 · MemoryOS: arXiv:2506.06326 · Memobase ·
  LightMem: arXiv:2510.18866
- MemoryBank: arXiv:2305.10250 · LUFY: arXiv:2409.12524 · FadeMem:
  arXiv:2601.18642 · RAPTOR: arXiv:2401.18059 · Rate-distortion compaction:
  arXiv:2607.08032 · HippoRAG: arXiv:2405.14831 / 2502.14802 · SleepGate:
  arXiv:2603.14517 · SCM: arXiv:2604.20943 · Larimar: arXiv:2403.11901
- Memory poisoning: MCFA arXiv:2603.15125 · MINJA arXiv:2601.05504 ·
  Dash et al. arXiv:2606.04329
- Surveys: arXiv:2404.13501, 2504.15965, 2505.00675, 2512.13564
- Temporal-DB prior art: Datomic excision/noHistory · XTDB DELETE/ERASE ·
  Kafka log compaction (Confluent) · RocksDB compaction · Kurrent snapshots ·
  Kleppmann, DDIA ch. 11-12
- HITL: Letta ADE docs · OpenAI memory controls · Mem0 dashboard ·
  hermes-agent #44963 · LangGraph HITL · Wikipedia pending/timed flagged
  revisions · GitHub suggested changes · CHI EA '25 agent-memory user study

## 14. Verification record (2026-07-16)

Six adversarial verifiers ran against rev 1, each attacking one dimension
with code reading, empirical scratch scripts against the project venv, and
web fact-checking. **46 findings: 17 HOLDS, 5 BROKEN, 24 NEEDS_AMENDMENT,
0 unverifiable.** All BROKEN and NEEDS_AMENDMENT findings are folded into
this rev 2.

Confirmed empirically (HOLDS): vec0 TEXT-metadata UPDATE + KNN tier filter on
sqlite-vec 0.1.9; `batch()` via BEGIN IMMEDIATE on the current connection
config; float32 embedding round-trip; CAS decide under a live two-writer
race; DEDUP-EXACT as-of invariance in isolation over a T grid; the
functional-slot machinery after both dedup and summarize; the console
fingerprint tripwire; packaging of the new package; every §1 file:line
citation. Citations: Letta #3116, sleep-time-compute amortization, the
generative-agents threshold of 150, the MemoryOS heat formula, and the
2607.08032 paper's existence are exact; the "no product stages memory
changes for approval" claim survived an active refutation attempt.

Refuted in rev 1 and fixed here (BROKEN): duplicate resurrection under later
ingest (→ duplicate-cascade, §4.0); stale summaries under later ingest
(→ staleness cascade, §4.3); MCP tools registered on a server the plugin
doesn't ship (→ §1.3.10, §6.3); unbacked config thresholds
(→ `MaintenanceConfig`, §3.6); `memory_clear` ghost-inode race presented as
mitigated (→ honest lease-based refusal, §7.3).

Amended: theorem scoped to the store predicate with the ingest-commutation
condition (§3.1); default-flag as-of is an is_latest surface (§1.1, §4.1);
versioned migration (§5); lease atomicity + heartbeat cadence (§7.2); lock
budget vs console retry stacking (§7.1); model work outside batch windows
(§7.1); auto-spawn Popen primitives and first-tool-call timing (§6.5);
apply-time target re-validation (§5); apply-time embedder ownership (§4.3);
recency-anchor alignment and last_access merge (§4.1, §4.4); explicit-only
promotion in v1 (§4.4); intra-run ordering (§4.4); slot-aware cursor (§6);
CLI naming/placement (§6.1); EngineGateway plumbing (§8); RAPTOR /
rate-distortion / poisoning citation precision (§2); test plan +7 scenarios
(§10).

### Second round (2026-07-16, rev 3)

A second, fully independent six-dimension adversarial pass ran against rev 2
(decision fidelity, code grounding, spec→plan coverage, workpackets
integrity, design attack, plan executability — every non-nit finding
re-verified by a dedicated skeptic; 15 verified: 10 CONFIRMED, 4 PARTIAL,
1 REFUTED). Grounding, decision fidelity, and coverage held clean; the
riskiest rev-2 empirical claims (executescript implicit commit, ALTER
duplicate-column on reopen, vec0 metadata UPDATE + KNN tier filter)
reproduced exactly. Confirmed and fixed in this rev 3:

- **BLOCKER — transitive duplicate-chain resurrection** (found independently
  by two reviewers; empirically reproduced three ways): a chain B→A→D left B
  permanently open after D's supersession, because the duplicate-cascade is
  single-level and rev 2's "depth 1" chain rule never re-pointed *existing*
  losers of a fact that itself becomes a loser — a wrong answer on the exact
  pure as-of surface the §3.1 theorem guarantees, which the single-level
  §10.2 test would have missed. Fix: `retire_duplicate` now re-points
  existing losers (§4.0); §10.2 gains the transitive case + a standing
  chain-invariant check.
- **Packet-boundary contradictions:** the console fingerprint update was
  triple-assigned (WP10a per §5/plan; WP10b per the old §9.2/§9.3/
  workpackets — the WP10b reading merges WP10a with a red console suite);
  now unambiguously v1/WP10a. The `EngineGateway` methods row in §9.3 is
  now marked (v1.1), matching §9.2/§8/plan/workpackets.
- **Seam fix:** `supersede_fact` now returns the full closed-id set so the
  §4.3 staleness cascade provably sees duplicate-cascade-closed sources.
- **§8.1** dropped a stray "auto-approve confidence band" lever that
  contradicted the nothing-auto-applies invariant (§3.3/§12).
- **Consistency:** EVICT's recency anchor is now verbatim the retriever's
  `last_access or valid_at` (rev 2's `max()` form diverged on future-dated
  facts); §6's triggers are numbered subsections so §6.x references resolve;
  citation nits (retriever.py:113-114; the salience mechanism is a non-user
  penalty, not a user reward).

Refuted (design already correct as written): the suspected fresh-create
migration trap — the `record_kind` ALTER lives only in the `<2` branch, never
in `SCHEMA_SQL`, and both create paths run clean. Downgraded on verification:
the staleness-cascade placement concern (the spec's "closed ids" wording was
already correct — the return-value contract above makes it unmissable) and
the WP10a lane-footprint understatement (WP10b is hard-serialized behind
WP10a, so no live race; the packet header now discloses the full footprint).
The companion plan was patched in the same rev for five executability gaps:
proposal-CRUD moved ahead of the runner (Task 2), hand-inserted fixtures for
the Task-3 stale-summary test, the console parity-pin update in Task 8, an
apply-path as-of grid re-run in Task 6, and the console suite runner command
(`console/.venv`, not the root venv).
