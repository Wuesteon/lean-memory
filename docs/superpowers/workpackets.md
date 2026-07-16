# Work Packets — parallel execution map

Date: 2026-07-10. Status: living document — update the status column as packets
move. Derived from the 2026-07 competitive analysis
(`docs/competitive-landscape.md`, `docs/benchmarks.md`), the approved strategy
(`docs/superpowers/specs/2026-07-08-strategic-direction-design.md`), and the
in-flight launch-gate plan
(`docs/superpowers/plans/2026-07-08-launch-quality-gate.md`).

## How to use this document

Each packet is sized for one git worktree + one focused session. To start one:

```bash
git worktree add ../lm-<branch> -b <branch> main   # fork point: main after WP0 merges
cd ../lm-<branch>
```

Then follow the superpowers flow **inside the worktree**: brainstorm if the
packet lists open design questions → `superpowers:writing-plans` to turn the
packet into a bite-sized implementation plan under `docs/superpowers/plans/` →
execute (subagent-driven or inline) → whole-branch review → merge via
`superpowers:finishing-a-development-branch`.

A packet is **claimed** by writing its branch name + date in the status table
below (single-writer per packet; the conflict lanes exist so packets never race
on the same files).

## Status table

| Packet | Branch | Lane | Blocked by | Gate | Status |
|---|---|---|---|---|---|
| WP0 Launch quality gate | `launch-gate` | C | — | — | **in flight** (LG tasks 2,3,4,7,8,9 done; 1,5,6,10,11 open) |
| WP1 Launch execution | `wp1-launch` | C | WP0 | spec §3 | open |
| WP2 Update-integrity bench | `wp2-update-integrity` | B | — | none — start anytime | open |
| WP3 Phase 2 runs + publication | `wp3-phase2-benchmarks` | B | WP0 | six-week read (spec §4) or "silence → retry lever" | open |
| WP4 Read-surface API | `wp4-read-api` | A | WP0 | six-week read ("no new engine work between launch and the read") | open |
| WP5 Deletion & GDPR (design → impl) | `wp5-deletion` | A | design: none; impl: WP4 | design anytime; impl six-week read | open |
| WP6 Scoping & filters | `wp6-scoping-filters` | A | WP4, WP5 | six-week read | open |
| WP7 Async API | `wp7-async` | A | WP4–WP6 | six-week read | open |
| WP8 Integrations & distribution wave | `wp8-*` (per sub-packet) | C | WP0 | six-week read (spec §5) | open |
| WP9 NLI middle tier (contingent) | `wp9-nli-resolver` | A | WP0 | **contingent** — see trigger | open |
| WP10a Sleep-time maintenance (engine + MCP review) | `wp10a-sleep-maintenance` | A | WP1 | — (conscious post-launch addition, recorded 2026-07-16) | **claimed** (2026-07-16: implementation started on `wp10a-sleep-maintenance` by user direction; **merge remains gated on WP1 launch**) |
| WP10b Maintenance review UI | `wp10b-review-ui` | D | WP10a | — | open |
| Memory UI | `worktree-memory-ui` | D | — | — | **MERGED** (2026-07-14: PR #2 → main 9d840b6; console 125 + core 141 green on merged main; lane D released) |

Lanes: **A** = engine/API surface (`src/lean_memory/` hot zone — strictly
sequential within the lane). **B** = benchmarks (`bench/` — parallel-safe with
A and C). **C** = launch/distribution (docs, packaging, listings). **D** = UI.

## Global invariants (every packet inherits these)

- Offline suite green at every commit: `.venv/bin/python -m pytest tests/ -q`
  — no test may require network or model downloads.
- Frozen-config discipline: any recalibrated constant is re-frozen in
  `bench/bet2_goldset.py` and gate-validated before the number is trusted; no
  published number without pinned config hash + judge model + judge prompt +
  backbone.
- ADD-only discipline: nothing mutates or deletes stored history except where
  WP5's approved design explicitly says so.
- Offline-by-default: every new backend/feature has a deterministic stub; a
  mandatory LLM dependency anywhere in the default path is a regression.
- License stays Apache-2.0. Commit messages follow the existing conventions.

## Dependency graph

```
WP0 launch-gate ──► WP1 launch ──► (six-week read, spec §4) ─┬─► WP3 benchmarks
                                                             ├─► WP4 ─► WP5(impl) ─► WP6 ─► WP7   (lane A, sequential)
                                                             └─► WP8 integrations
WP1 launch ──► WP10a sleep-maintenance (lane A — serialize with WP4+) ─► WP10b review UI (lane D)
WP2 update-integrity ────────────── independent, anytime (lane B)
WP5 design doc ──────────────────── independent, anytime (docs only)
WP9 NLI tier ────────────────────── contingent on WP0 Task-6 / WP3 telemetry
```

The six-week-read gate is a **decision point, not a hard wall**: the strategy
spec reserves engine work until the demand signal is read, and makes WP3 the
first move on "signal" *and* the retry lever on "silence". Starting a gated
packet early is a conscious strategy change — record it here if taken.

---

## WP0 — Launch quality gate (in flight)

**Branch:** `launch-gate` (this is the existing plan, not a new worktree).
**Plan:** `docs/superpowers/plans/2026-07-08-launch-quality-gate.md`.

**COMPLETE (2026-07-11/12):** all tasks done — merged to `main` (b4acb29),
secrets rotated 2026-07-11 (revoked in dashboards, local copies deleted),
stale branch labels removed locally and on origin. Post-gate, a publish-
readiness review board found three launch blockers on the v0.1.2 MCP first-run
path; the fixes ship as v0.1.3 (see CHANGELOG).

Already landed on this branch: escalation probe with `--json` (`496c1a7`),
baseline sweep — best point 95.9% at 0.3/0.3, coref floor 65.6% (`31ec8ad`),
endpoint-scoped coref router fix (`c02ae4f`, goldset unchanged at 10.1%),
`Memory.search(now=...)` recency anchoring (`80246db`), ungated Qwen3 embedder
default (`c2cdfca`), MCP-first README + GIF (`4879e09`).

**Everything else forks from `main` after WP0's Task 11 merges.**

---

## WP1 — Launch execution

**Branch:** `wp1-launch` · **Blocked by:** WP0 · **Effort:** S (mostly
out-of-repo actions + small metadata commits)

**Goal:** Execute spec §3 — the six listing channels, one narrative
("local-first, no server, time-travel history"), tagged release + CHANGELOG.

**Scope (in):** MCP Registry listing; `awesome-mcp-servers` PR; Claude Code
plugin marketplace submission; PyPI metadata polish (keywords, badges, project
URLs in `pyproject.toml`); Show HN + r/ClaudeAI + r/LocalLLaMA post drafts;
`CHANGELOG.md` + first tagged release. Listing/post drafts live in
`docs/launch/` so they get reviewed like code.
**Scope (out):** any engine change; any benchmark claim in launch copy (we
launch with *no* number by design — spec §1).

**Files:** `pyproject.toml`, `CHANGELOG.md`, `README.md` (badges only),
`docs/launch/*.md` (new). No `src/` changes → conflict-free with lane A/B.

**Acceptance criteria:**
- Release `v0.x` tagged; CHANGELOG covers Phase 0→launch-gate honestly.
- All six channel submissions live or submitted, each linking the same README
  quickstart; copy contains zero benchmark numbers and states the model
  download size.
- Risks from the spec addressed in copy: differentiation vs OpenMemory ("no
  Docker, no server, single file, temporal queries"), positioning vs built-in
  Claude Code memory ("cross-agent, portable, auditable").

**Verification:** fresh-machine (or fresh venv) walkthrough of each published
listing's install snippet ends in a working `memory_add`/`memory_search`.

---

## WP2 — Update-integrity benchmark (the supersession head-to-head)

**Branch:** `wp2-update-integrity` · **Blocked by:** nothing · **Effort:** S–M

**Goal:** A small, honest, reproducible test of the one capability we claim as
core: *when a fact changes, does the engine return the current truth and keep
the old one queryable?* This is launch-supporting evidence, not a leaderboard
score — it needs no LLM judge and no frozen backbone.

**Why (evidence):** Mem0 OSS dedup is an exact-MD5 hash check; semantic
conflict resolution was requested in mem0ai/mem0#4896 and closed "not
planned" — contradictory facts accumulate side by side. Their docs still
promise "latest truth wins". Zep does this correctly but requires a graph
server. No local-first peer publishes update-integrity evidence at all. See
`docs/competitive-landscape.md`.

**Scope (in):**
- `bench/update_integrity.py`: ~10 scripted scenarios through the public API
  only (`mem.add` → `mem.search`): name change (#4896's exact case), employer
  change, city move, preference flip, additive vs contradictory ("also" case
  must EXTEND, not supersede), point-in-time readback via `as_of`, restart
  persistence (close + reopen).
- Per scenario, assert: top-1 is the new fact; superseded fact has
  `is_latest=False` + `superseded_by` set; `as_of=<t before update>` returns
  the old fact. Emit a markdown results table.
- `tests/test_update_integrity_scenarios.py`: the same scenarios offline
  (deterministic stubs) as regression tests — supersession is resolver logic
  and must hold without real models.
- Optional arm (separate flag, documented): the same scenario script run
  against `mem0ai` OSS for a side-by-side table. Requires their LLM path
  (Ollama or API key) — keep it opt-in and record their exact version.
**Scope (out):** LongMemEval/LoCoMo anything (that is WP3); publishing claims
about Mem0 beyond reproduced, versioned behavior.

**Files:** `bench/update_integrity.py` (new),
`tests/test_update_integrity_scenarios.py` (new), results appendix section in
`docs/competitive-landscape.md`. Zero overlap with lanes A/C.

**Acceptance criteria:** all scenarios pass offline AND with `[models]`
backends; the results table renders from a single command; if the Mem0 arm is
run, its version + config are pinned in the output.

**Verification:** `.venv/bin/python bench/update_integrity.py --markdown` and
`.venv/bin/python -m pytest tests/test_update_integrity_scenarios.py -q`.

---

## WP3 — Phase 2 benchmark runs + publication

**Branch:** `wp3-phase2-benchmarks` · **Blocked by:** WP0 (frozen constants)
· **Gate:** six-week read — first move on "signal", retry lever on "silence"
· **Effort:** L (wall-clock heavy, mostly unattended)

**Goal:** lean-memory's first public numbers: LongMemEval KU slice + LoCoMo
temporal slice under the frozen-judge discipline, plus the Key Experiment
(`is_latest_only` A/B) — the evidence for the supersession story WP2 only
smoke-tests.

**Why (evidence):** every vendor number in this category is contested (Mem0 ↔
Zep dispute, MemPalace's maintainer-conceded retractions, LoCoMo's ~6.4%-wrong
answer key — see `docs/benchmarks.md`). No local-first peer publishes
frozen-config results. The concrete same-architecture bar: AIngram's
self-reported real-noisy LongMemEval-S recall_any@10 = 0.955 / 72.8% e2e with
gpt-4o-mini (weakest on temporal reasoning, 60.2% — our home turf).

**Scope (in):**
- Re-run per the handoff runbook (`docs/superpowers/phase2-HANDOFF.md`): fresh
  config hash over the WP0-frozen constants, ingest (resume the paused HF
  Space `wuesteon1337/lm-typer-phase2` or any CUDA box — typer digest pinned
  in manifests), then read → judge → aggregate.
- Key Experiment: paired-bootstrap A/B on `is_latest_only` (harness already
  implements it).
- Two cheap ablation arms while the corpus is hot: reranker on/off and
  recency-term on/off (small `phase2_eval.py` arm additions; the vstash
  negative result on rerankers was refuted as evidence for our setting, but
  our setting is untested — answer it ourselves).
- Publication: results in `bench/results/phase2/`, a results section in
  `docs/benchmarks.md` reporting **oracle and real-noisy splits separately**
  (adopt AIngram's split template; never headline a recall@k as if it were
  end-to-end QA — that error is what discredited MemPalace), multi-run
  intervals, config hashes inline. Remove the README status caveat.
**Scope (out):** chasing any vendor's headline number; engine changes (if the
runs expose new flaws, STOP and write the finding — same discipline as
2026-07-03).

**Files:** `bench/results/phase2/` (new), `bench/phase2_eval.py` (ablation
arms), `docs/benchmarks.md`, `README.md` (status note). No `src/` changes.

**Acceptance criteria:** result files with pinned config hashes for both
slices; Key Experiment verdict with CIs; ablation table; docs updated; ingest
telemetry shows escalation at the WP0-frozen operating point (<20%) — if not,
that is a STOP finding, not a footnote.

**Verification:** `bench/phase2_eval.py` refuses to aggregate incomplete runs
(built-in); the published section links every number to its result file.

---

## WP4 — Read-surface API (`history` / `get` / `get_all` / `explain`)

**Branch:** `wp4-read-api` · **Blocked by:** WP0 · **Gate:** six-week read
· **Effort:** M · **Lane A — claims `memory.py` + `store/` first**

**Goal:** Expose what the spine already stores. The ADD-only monotemporal
model is the differentiator; today `Memory` offers no way to *see* it besides
search. This is also the API-parity tier every competitor ships (Mem0 OSS:
`get/get_all/history` — see `docs/competitive-landscape.md`).

**Scope (in):**
- `Memory.get(namespace, fact_id) -> Fact | None`
- `Memory.get_all(namespace, *, subject=None, predicate=None,
  is_latest_only=True, limit=100, offset=0) -> list[Fact]`
- `Memory.history(namespace, fact_id) -> list[Fact]` — the full supersession
  chain (walk `superseded_by` in both directions), oldest→newest.
- `search(..., explain=True)` → each `RetrievedFact` carries its score
  decomposition (dense/sparse ranks, RRF, rerank, recency, importance, final —
  the retriever computes all of these already; this exposes, not computes).
- MCP parity: `memory_history` + `memory_get_all` tools in `mcp_server.py`.
- README "inspect your memory" section (the pitch is *auditable* memory — show
  it).
**Scope (out):** any write-path change; deletion (WP5); scoping (WP6).

**Files:** `src/lean_memory/memory.py`, `src/lean_memory/store/base.py`,
`src/lean_memory/store/sqlite_store.py`, `src/lean_memory/retrieve/retriever.py`
(explain only), `src/lean_memory/mcp_server.py`, `tests/test_read_api.py`
(new), `README.md`.

**Acceptance criteria:** history of the README's Acme→Globex example returns
both facts in order with correct `is_latest`/`superseded_by`; `get_all`
paginates deterministically; explain fields sum/compose to `final_score`;
offline suite green; MCP tools return JSON-serializable payloads.

**Verification:** `.venv/bin/python -m pytest tests/test_read_api.py tests/ -q`
plus a 5-line REPL walkthrough recorded in the PR description.

---

## WP5 — Deletion & GDPR (design first, then implement)

**Branch:** `wp5-deletion` · **Design blocked by:** nothing (docs-only —
can run anytime) · **Impl blocked by:** WP4 + approved design · **Effort:**
design S, impl M

**Goal:** A designed answer to "right to be forgotten" that coexists with
ADD-only. This is the one table-stakes gap that is a real design problem, and
it blocks the privacy positioning (every competitor ships `delete`; Cognee
ships `forget`).

**Phase 1 — design spec (`docs/superpowers/specs/YYYY-MM-DD-deletion-design.md`),
via brainstorming.** Options to evaluate, minimum:
(a) namespace purge — delete the SQLite file (already trivially true; document
it as the tenant-level answer);
(b) per-fact hard delete with cascade (fact + vectors + FTS row + episode if
orphaned) — breaks the audit spine for that fact, by explicit user intent;
(c) redaction — overwrite `fact_text`/`object_literal`/embedding with
tombstone values, keep the spine row and supersession pointers intact;
(d) crypto-shredding — per-namespace or per-subject key, delete the key.
Decide: default semantics of `delete`, what `history()` shows afterwards, what
happens to a supersession chain crossing a deleted fact, and MCP exposure.

**Phase 2 — implement per the approved spec** (own writing-plans plan).

**Files (impl, expected):** `memory.py`, `store/*`, `mcp_server.py`,
`tests/test_deletion.py` — same lane-A hot zone as WP4, hence the ordering.

**Acceptance criteria (impl):** deletion semantics match the approved spec
exactly; a deleted fact is unrecoverable from the DB file (verified by raw
SQLite inspection, not just API behavior); `as_of` queries never resurrect it;
offline suite green.

---

## WP6 — Scoping & filters (`user/agent/run`, metadata, TTL)

**Branch:** `wp6-scoping-filters` · **Blocked by:** WP4, WP5 · **Gate:**
six-week read · **Effort:** L (touches the schema)

**Goal:** Close the remaining conventional-API gaps: three-axis scoping
(`user_id`/`agent_id`/`run_id` — agent memories as first-class, the de-facto
convention), metadata on `add` + filter operators on `search`/`get_all`
(`eq/ne/gt/lt/in/nin/contains` + AND/OR/NOT), per-fact `expiration_date` with
`show_expired`.

**Open design questions (brainstorm at execution):**
- Scoping as composite namespaces (keeps one-file-per-tenant isolation, no
  schema change) vs. columns inside one store (cross-scope queries, needs
  migration). The per-namespace-file isolation is a stated differentiator —
  don't trade it away silently.
- Schema versioning/migration story for existing user DB files (first release
  with persisted-format changes — needs a `schema_version` pragma + upgrade
  path).
**Scope (out):** REST/TS surface (WP8); async (WP7).

**Files:** `src/lean_memory/types.py`, `store/schema.py`, `store/sqlite_store.py`,
`memory.py`, `mcp_server.py`, `tests/test_scoping.py`, `tests/test_filters.py`.

**Acceptance criteria:** old DB files open and work unchanged (regression test
against a checked-in fixture DB); filter grammar documented in README; TTL
respected by search and `get_all` unless `show_expired=True`.

---

## WP7 — Async API

**Branch:** `wp7-async` · **Blocked by:** WP4–WP6 (mirror a stable surface
once, not three times) · **Gate:** six-week read · **Effort:** M

**Goal:** `AsyncMemory` mirroring every public method (the convention Mem0
set; agent frameworks are async-first). Design constraint: SQLite is
sync — decide executor-offload (`asyncio.to_thread`) vs. `aiosqlite` at
brainstorm; embedding/rerank calls are the actual latency and offload cleanly.

**Files:** `src/lean_memory/aio.py` (new), `tests/test_async_memory.py` (new),
README section. Minimal overlap with WP4–6 once their surface is merged.

**Acceptance criteria:** every public `Memory` method has an async twin with
identical semantics (shared test parametrization, not copied tests); no event
-loop blocking >50ms in the async path with stub backends.

---

## WP8 — Integrations & distribution wave (post-read, sub-packets)

**Gate:** six-week read (spec §5 defers all of these) · Each sub-packet is its
own worktree; they share no files with lane A.

| Sub-packet | Branch | What | Evidence for priority |
|---|---|---|---|
| 8a Auto-capture hooks | `wp8a-hooks` | Claude Code / Cursor hooks that save memory before context compaction; recall-on-start | The widest-reach integration pattern observed (MemPalace ships hooks for 3 IDEs/CLIs) |
| 8b REST server | `wp8b-rest` | Thin FastAPI wrapper over `Memory`, optional extra | Table-stakes for non-Python callers; Mem0 ships one |
| 8c TypeScript client | `wp8c-ts` | TS SDK speaking to 8b (or MCP) | Mem0 mirrors full surface in TS; agent dev is TS-heavy |
| 8d Framework adapters | `wp8d-adapters` | LangChain + LlamaIndex memory classes | Standardized memory interfaces are the swap-in point |

**Rule:** each sub-packet starts with its own mini-spec; none may add a
mandatory server/daemon to the core library (the positioning is the product).

---

## WP9 — NLI middle tier in the contradiction ladder (CONTINGENT)

**Branch:** `wp9-nli-resolver` · **Trigger — start ONLY if:** WP0 Task 6
cannot reach a <20% operating point that passes all three BET-2 gates, **or**
WP3 ingest telemetry shows the LLM share of per-turn wall time is still the
bottleneck at the frozen point.

**Goal:** A local NLI model (DeBERTa-v3-class, ONNX, offline) between the
cheap resolver steps (slot/cosine/subsumption) and LLM escalation in
`extract/contradiction.py` — the design AIngram used to avoid LLM escalation
entirely. Offline stub mandatory; `[extract]`-style opt-in extra for the real
model.

**Files:** `src/lean_memory/extract/contradiction.py`, new
`extract/nli.py` + stub, `tests/test_nli_resolver.py`, BET-2 gate re-run.

**Acceptance criteria:** escalation at target with gates green; added p50
latency per `add` < 150ms on CPU with the real model; zero new mandatory deps.

---

## WP10a — Sleep-time maintenance: engine + MCP review

**Branch:** `wp10a-sleep-maintenance` · **Blocked by:** WP1 (strictly
post-launch; a conscious strategy addition recorded 2026-07-16, not gated on
the six-week read) · **Effort:** L (~1.5 weeks) · **Lane A — claims
`memory.py` + `store/*` + `types.py` + `retrieve/retriever.py` +
`mcp_server.py`; serialize with WP4–WP7 within the lane. Also touches, for
the MCP/packaging surfaces only: `console/.../observe_mcp.py`,
`console/.../routes/mcp.py`, `console/.../inspect_sql.py` (fingerprint
constant only — the schema-v2 DDL trips it, spec §5), `plugin/`,
`server.json`. No UI files; WP10b is hard-serialized behind this packet, so
the console-file overlap cannot race.**

**Design (approved, verified):**
`docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md` (rev 3)
— read it in full before starting; it carries a two-round verification
record (46 findings vs rev 1; an independent second round vs rev 2 whose one
blocker — transitive duplicate-chain resurrection — is fixed in rev 3) and
the resolved §12 decisions.
**Plan:** `docs/superpowers/plans/2026-07-16-sleep-time-maintenance.md`.

**Goal:** Offline "sleep-time" maintenance between sessions — dedupe,
summarize-old, evict-low-value — preserving the ADD-only spine and as-of
semantics (spec §3.1 theorem incl. ingest commutation), with a staged human
review queue served two ways: console UI (WP10b) and conversational review
through Claude Code via MCP tools + a shipped review prompt.

**Scope (in):** store verbs + `batch()` + engine busy_timeout; schema v2
(user_version-gated migration + v1 fixture); the two ingest hooks
(duplicate-cascade, summary-staleness cascade); DEDUP-EXACT auto;
EVICT auto-band + proposals; DEDUP-NEAR + SUMMARIZE proposals (extractive
stub default, `[llm]` Ollama opt-in); tier filters + `include_cold`;
`MaintenanceConfig`; lease/ledger/runner + CLI `lean-memory-maintain`;
4 MCP tools on all three MCP surfaces + review prompt + plugin command file
+ the 4 `EngineGateway` maintenance methods (the console MCP surfaces write
only through the gateway — boundary corrected during implementation);
opt-in auto-spawn.
**Scope (out):** console UI (WP10b); physical space reclamation (v2,
design-first); deletion of any kind (WP5's problem).

**Interactions:** shares the schema-migration anchor obligation with WP6
(whoever ships first restructures `_init_schema` into versioned branches and
checks in the v1 fixture DB — coordinate); WP4's `history()` must distinguish
retirement-by-duplication edges from world-time supersession (spec §4.1).

**Acceptance criteria:** spec §10 test plan green (as-of grid at the store
predicate, incl. the post-apply re-run; resurrection — incl. the transitive
chain variant — + stale-summary regression tests; ingest hooks
byte-identical no-ops pre-maintenance; proposal lifecycle incl. stale-target
expiry; migration fixture; stdout hygiene incl. spawned child); offline suite
green; existing spine/as-of pins untouched.

---

## WP10b — Maintenance review UI (console)

**Branch:** `wp10b-review-ui` · **Blocked by:** WP10a (decided 2026-07-16:
starts right after WP10a merges, not gated on the demand read) · **Effort:**
M (~3-4 days) · **Lane D + console files.**

**Goal:** The next-morning click-through: a `Review` page listing pending
proposals grouped by entity with before/after evidence; verbs approve /
keep / edit-then-approve / promote; batch approve per group.

**Files:** `console/.../inspect_sql.py` (proposal reads —
the schema fingerprint lands in WP10a with the migration, spec §5; the 4
`EngineGateway` maintenance methods likewise land in WP10a, which this
packet consumes), new review router, `ui/src/pages/Review.tsx`, `ui/src/App.tsx`,
`ui/src/components/Layout.tsx`, `ui/src/api.ts`, console + UI tests.

**Acceptance criteria:** decisions round-trip through `EngineGateway` (never
raw SQL writes); CAS "already decided elsewhere" surfaced in the UI; console
suite green; spec §8.1 fatigue levers present (entity grouping, batch
approve, budget cap).

---

## Anti-goals (decided, with evidence — do not open packets for these)

- **Graph memory / graph-traversal retrieval channel.** Mem0's own published
  ablation: graph variant +1.6 points overall, ~3× slower search, ~2× token
  cost — then removed from their OSS entirely. Revisit only if WP3 shows
  systematic multi-hop failures.
- **Hosted/managed consolidation services, webhooks, dashboards, SOC2, hosted
  anything.** Managed-service concerns; the embedded positioning sidesteps
  them (per-spec out-of-scope). *Amended 2026-07-16: the original bullet
  banned all "consolidation/summarization passes"; its stated rationale
  (managed-service concerns) does not apply to a local, embedded, default-off
  sleep-time maintenance job, which is now in scope as WP10a/WP10b (verified design:
  `docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md`).
  Hosted consolidation stays out.*
- **CRDT multi-agent sync.** sqlite-memory's differentiated lane; real demand
  signal required before entering it.
- **int8 vectors / LanceStore.** Blocked upstream / deferred (ARCHITECTURE.md,
  spec §5).

## Source documents

- Competitive evidence: `docs/competitive-landscape.md`, `docs/benchmarks.md`
- Strategy + gate: `docs/superpowers/specs/2026-07-08-strategic-direction-design.md`
- Engine postmortem: `docs/phase2-learnings.md`
- Benchmark runbook: `docs/superpowers/phase2-HANDOFF.md`
- In-flight plan: `docs/superpowers/plans/2026-07-08-launch-quality-gate.md`
