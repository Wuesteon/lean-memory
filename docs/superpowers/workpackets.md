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
| WP0 Launch quality gate | `launch-gate` | C | — | — | **COMPLETE** (2026-07-12: all tasks done, merged to `main` b4acb29; v0.1.3 readiness-board fixes; see section) |
| WP1 Launch execution | `wp1-launch` | C | WP0 | spec §3 | **DONE — required channels live; posts declared optional** (2026-07-21: PR #5 → main 0044280; v0.2.1 tagged on merge; MCP Registry + PyPI core+console LIVE. 2026-07-29 user decision: remaining out-of-repo channels — awesome-mcp PR #9890, plugin-marketplace form, Show HN, subreddit posts — are OPTIONAL, not launch-required; demand read runs on the live channels from 2026-07-29 (v0.2.2). Runbook: `docs/launch/launch-checklist.md`) |
| WP2 Update-integrity bench | `worktree-wp2-update-integrity` | B | — | none — start anytime | **MERGED** (2026-07-29: PR #9 → main c803e54; 10 scenarios, 26/26 assertions PASS; core 301 + console 153 green; results appendix in `docs/competitive-landscape.md`; optional mem0 arm designed in the plan, not run; lane B released. **mem0 arm MERGED 2026-08-07** (PR #22 → main dddb36e, closes [#15](https://github.com/Wuesteon/lean-memory/issues/15); `--arm mem0` per the frozen Task 5 design + live local run — mem0 2.0.17 on Ollama qwen2.5:3b, full retrieval backend, table byte-identical across nine runs; results + caveats in the `docs/competitive-landscape.md` appendix; reproduce recipe requires the mem0ai[extras]+[nlp] venv + Ollama) |
| WP3 Phase 2 runs + publication | `wp3-phase2-benchmarks` | B | WP0 | six-week read (spec §4) or "silence → retry lever" | open |
| WP4 Read-surface API | `wp4-read-api` | A | WP0 | six-week read ("no new engine work between launch and the read") | open |
| WP5 Deletion & GDPR (design → impl) | `wp5-deletion` | A | design: none; impl: WP4 | design anytime; impl six-week read | design **DONE 2026-08-06** (spec: `docs/superpowers/specs/2026-08-06-wp5-deletion-gdpr-design.md`, adversarially reviewed rev 2, all claims measured against v0.2.3; closes [#17](https://github.com/Wuesteon/lean-memory/issues/17); maintainer sign-off items in spec §14); impl still gated |
| WP6 Scoping & filters | `wp6-scoping-filters` | A | WP4, WP5 | six-week read | open |
| WP7 Async API | `wp7-async` | A | WP4–WP6 | six-week read | open |
| WP8 Integrations & distribution wave | `wp8-*` (per sub-packet) | C | WP0 | six-week read (spec §5) | open |
| WP9 NLI middle tier (contingent) | `wp9-nli-resolver` | A | WP0 | **contingent** — see trigger | open |
| WP10a Sleep-time maintenance (engine + MCP review) | `wp10a-sleep-maintenance` | A | WP1 | — (conscious post-launch addition, recorded 2026-07-16) | **MERGED** (2026-07-17: PR #3 → main d93b326; core 282 + console 138 green on merged main; final whole-branch review 0 Critical / 0 Important; merged ahead of WP1 by conscious user decision — safe: feature default-off, first-run path pinned byte-identical. WP10b unblocked; its carry-ins recorded in its section) |
| WP10b Maintenance review UI | `wp10b-review-ui` | D | WP10a | — | **MERGED** (2026-07-17: PR #4 → main f082f4e; core 284 + console 152 green on merged main; all four WP10a carry-ins closed; lane D released) |
| Memory UI | `worktree-memory-ui` | D | — | — | **MERGED** (2026-07-14: PR #2 → main 9d840b6; console 125 + core 141 green on merged main; lane D released) |
| WP12 mcp 2.0 migration | `worktree-wp12-mcp2-migration` | A | — | none — dependency-driven | **MERGED** (2026-07-29: PR #10 → main 4efe4ca; dual-path compat, pin widened to mcp>=1.2,<3; fixed the 2.0 worker-thread SQLite crash (check_same_thread=False, serialized); all four suite combos green locally, CI green on fresh-resolved 2.0; shipped in v0.2.3 (2026-07-29); lane A released) |
| WP11 Write-time restatement dedupe | `worktree-wp11-restatement-dedup` | A | — | — | **MERGED** (2026-07-29: PR #7 → main 52d21fd; core 289 + console 153 green on merged branch; rode along: mcp>=1.2,<2 pin for the mcp 2.0.0 fastmcp removal, gated WP9 LLM-judge design doc; lane A released) |
| WP14 Console tool metadata | `wp14-console-tool-metadata` | D | — | precondition: console listed on Glama? | **MERGED** (2026-08-06: PR #19 → main 9586b59, closes [#13](https://github.com/Wuesteon/lean-memory/issues/13); honest annotations + 100% param descriptions + when-to-use guidance on all 6 console tools, both surfaces — shared registration makes stdio/HTTP parity structural; contract pinned in `console/tests/test_mcp_tool_metadata.py` (45 tests, wire-name asserts); console 198 green on mcp 1.28.1 AND 2.0.0 scratch venv, live stdio wire check on both majors; core untouched (319); CI 6/6; precondition answered NO — console not independently listed on Glama (listings are repo-keyed), done as hygiene/parity; deliberate core divergences recorded in the section; unreleased — ships with the next tag; lane D released) |
| WP15 Entity name collation (`name_key`) | `wp15-entity-collation` | A | — (design done) | ~~six-week read~~ **gate waived by maintainer 2026-08-07** (conscious strategy change, recorded per the gate rule: recommendation approved + lane-A gate waived; sequence before WP4 preserved) | **MERGED** (2026-08-07: PR #21 → main e175afa, closes [#14](https://github.com/Wuesteon/lean-memory/issues/14); option (c) of `docs/superpowers/specs/2026-08-06-entity-case-collation-decision.md` — `entity.name_key` + schema v3 atomic migration + shared `lean_memory/normalize.py` + extractor `re.I`; supersedes WP11's case-split known limit; core 346 + console 198 green, CI 6/6; unreleased — ships with the next tag; lane A released) |
| WP13 MCP tool metadata (Glama audit) | `worktree-wp12-mcp-tool-metadata` | A | — | — | **MERGED** (2026-07-29: PR #11 → main 342461e; branch name predates the renumber — claimed as WP12 concurrently with the mcp 2.0 migration; annotations + param descriptions + usage guidance on all 7 server tools, no behavior change beyond k/limit schema minimums; contract pinned in `tests/test_mcp_tool_metadata.py`, green on both mcp majors (core 319 on 1.28.0, 318+1 skip on 2.0.0 scratch venv), console 153, CI 6/6; shipped in v0.2.3 (2026-07-29) — Glama re-scan triggered; lane A released) |

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

## WP0 — Launch quality gate (complete)

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

## WP1 — Launch execution (in-repo work merged; out-of-repo channels in progress)

**Status (2026-07-21):** PR #5 merged to `main` (0044280), v0.2.1 tagged and
released on the merge; registry + PyPI live at 0.2.1. Remaining channel
actions and their states: `docs/launch/launch-checklist.md`.

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
- **Carry-in from WP2 (2026-07-29):** a point-in-time read today requires
  `search(..., as_of=T, is_latest_only=False)` — the latest-only filter
  applies even under `as_of`, so superseded facts stay invisible unless the
  caller knows to open it (bit the WP2 scenario engine; documented in the
  `docs/competitive-landscape.md` appendix). `history()` / a dedicated
  point-in-time verb should own this so callers can't hold it wrong.
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

**Variant recorded 2026-07-29 (user-requested):** an optional API-key
LLM-as-judge backend for the same ambiguous-band rung, via the existing
`classify(..., llm_typer=...)` seam — design in
`specs/2026-07-29-wp9-llm-judge-design.md`. Adjudicates EXTENDS vs SUPERSEDES
only (never gates the WP11 restatement skip — a wrong skip loses data, a
wrong adjudication is ADD-only-recoverable); enabled by
`LEAN_MEMORY_JUDGE_API_KEY` presence, fails open to `ambiguous_default`.
Implementation stays behind this packet's gate; the two implementations (local
NLI / API judge) compose, order decided when the packet opens.

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

**Carry-in cleanups from WP10a's final whole-branch review — ALL CLOSED in WP10b (PR #4)** (small; touch
files this packet already opens): move the proposal-budget check ahead of the
summarizer invocation in `maintain/transforms.py` (matters once Ollama is the
`[llm]` summarizer); add an exists-guard so CLI dry-run against an explicit
nonexistent `--namespace` doesn't create an empty `<ns>.db`; declare `path`
on the Store ABC; document the `memory_maintenance_run(apply=True)`
(auto band + stages) vs auto-spawn `--auto-only` (auto band only) asymmetry
in the tool docstring.

---

## WP11 — Write-time restatement dedupe (bugfix)

**Claimed 2026-07-29** on `worktree-wp11-restatement-dedup`, user-directed in
response to the lean-memory-sim `longrun-18` study (2026-07-29 report):
identical restatements of a fact accumulate as duplicate latest facts. Cause:
`ContradictionResolver.classify` deliberately excludes exact-text restatements
from contradiction consideration (correct — a restatement supersedes nothing),
but `Memory.add()` pass 5 then persists the fact unconditionally, so every
verbatim restatement inserts a new latest row in the same slot. Store size and
retrieval noise grow linearly with conversational repetition; WP10a's
`dedup_near` can only clean it up offline, default-off, human-reviewed.

**Scope:** in `Memory.add()` pass 5, skip persisting a typed fact whose
normalized `fact_text` (casefold + collapse whitespace + strip edge
punctuation; `_restatement_key`) matches a latest fact in the same
`(subject, predicate)` slot. Normalization is deterministic on purpose: the
default stub embedder's similarity bands can't be trusted to catch trivial
formatting variants, and a variant landing in the ambiguous band stacks a
second co-valid latest row (empirically observed). ADD-only compatible
(nothing stored is mutated or deleted — the write is simply not made). Not
gated on the six-week read: this is a bugfix on the existing write path, not
new engine surface.

**Non-goals:** internal-punctuation / semantic near-dupes — meaning-bearing
("10,5" vs "105"), so they stay with contradiction resolution and WP10a's
offline `dedup_near` band; re-assertion bookkeeping (seen-counts,
last-asserted timestamps) — design it in WP4+ if the demand read asks for it.

**Known limit — SUPERSEDED by WP15 (2026-08-07).** As shipped, a case variant of
the ENTITY surface form ("acme" vs "Acme") resolved to a different entity
(`upsert_entity` matched `name=?` under BINARY collation) → different slot →
bypassed dedupe AND contradiction resolution entirely, and that was pinned in
`tests/test_restatement_dedupe.py::test_entity_case_variant_splits_the_slot_known_limit`.
WP15 took the collation decision (real distinct-by-case counterexamples:
"Polish"/"polish") and closed it: identity now resolves on `entity.name_key`, so
variants share one slot and the WP11 skip applies to them. That test is deleted;
the replacement known limit — genuinely case-distinct subjects now MERGE, and
why that is the accepted trade (recoverable via supersession) — is pinned in
`tests/test_entity_collation.py::test_case_distinct_subjects_merge_known_limit`.
See §WP15.

---

## WP12 — mcp SDK 2.0 migration (dual-path)

**Merged 2026-07-29** (PR #10; shipped in v0.2.3). Plan:
`docs/superpowers/plans/2026-07-29-wp12-mcp2-migration.md`. Compat layers
`lean_memory._mcp_compat` + `lean_memory_console._mcp_compat` (duplicated on
purpose — console↔core version skew makes cross-package private imports
fragile) handle the 2.0 `FastMCP` → `MCPServer` rename, ctor `version=`,
transport params moving to `streamable_http_app(...)`, and the in-memory test
client (`tests/mcp_client_compat.py`). Also fixed the 2.0 worker-thread
SQLite crash (`check_same_thread=False`, serialized-safe for serial MCP
traffic). Pin: `mcp>=1.2,<3` in both packages, cap asserted by the pyproject
guard test.

**Future decision (recorded, no date; tracked as [#16](https://github.com/Wuesteon/lean-memory/issues/16)):** drop the 1.x path and floor at
`mcp>=2` once the ecosystem has moved — signal to watch: major MCP clients /
frameworks flooring at 2.x, or the 1.x branch stopping security fixes. Until
then both paths stay tested (dev venvs on 1.x, CI fresh-resolve on 2.x).

**Signal checked 2026-08-06: NOT fired.** Every major consumer still caps at
`<2` (fastmcp-slim 3.4.6 `mcp<2.0,>=1.24.0`; langchain-mcp-adapters 0.3.2 —
released that same day — `mcp<2.0.0,>=1.24.0`; openai-agents 0.19.4 `mcp<2`;
llama-index-tools-mcp 0.4.8 `mcp<2`), 1.29.0 shipped the same day as 2.0.0,
and upstream's migration page explicitly advises library maintainers to keep
an upper bound `<2`. No 1.x EOL announced. Re-check ~monthly; earliest
plausible flip is a fastmcp 4.x.

---

## WP13 — MCP tool metadata (Glama audit)

**Merged 2026-07-29** (PR #11; shipped in v0.2.3 — the Glama re-scan only
counts released artifacts, so the score signal dates from this tag). Claimed
concurrently with WP12 under the same number and renumbered at merge; branch
name `worktree-wp12-mcp-tool-metadata` predates the renumber. Annotations
(`readOnlyHint`/`destructiveHint`/`idempotentHint`, `openWorldHint=False`),
100% parameter-description coverage, and when-to-use guidance on all 7 core
server tools; only behavior change is schema minimums on `memory_search.k` /
`memory_review_queue.limit`. Contract pinned in
`tests/test_mcp_tool_metadata.py`, asserted via camelCase wire names
(`model_dump(by_alias=True)`) so it holds on both mcp majors.

---

## WP14 — Console tool metadata (lane D, open)

**Branch:** `wp14-console-tool-metadata` · **Blocked by:** — · **Gate:**
none, but **precondition:** check whether `lean-memory-console` is
independently listed/scored on Glama — if it isn't, this drops in priority.

**Goal:** Give the console's MCP wrapper tools (stdio `observe_mcp.py` +
HTTP mount `routes/mcp.py`, via shared `mcp_tools.py`) the same annotation +
parameter-description + usage-guidance treatment as WP13's core tools,
reusing the WP13 contract-test pattern nearly verbatim
(`console/tests/test_mcp_tool_metadata.py`, wire-name asserts, both majors).

**Files:** `console/src/lean_memory_console/mcp_tools.py`, `observe_mcp.py`,
`routes/mcp.py`, new console contract test. Lane D — no core files.

**Recorded during implementation (deliberate divergences from core WP13):**

- **`memory_search` is `readOnlyHint=False` on the console, `True` on core.**
  MCP's `readOnlyHint` means "does not modify its ENVIRONMENT". The console
  wrapper is an *observing* superset: `gateway.search()` unconditionally
  appends a `'search'` row to `_events.db` (pruning past CAP=10 000) and
  creates the namespace store file on first touch. Core's `memory_search` has
  no event log, so `True` is truthful there. One standard is applied to every
  console tool — no telemetry carve-out — which is why `memory_search` and
  `memory_review_queue` (lazy proposal expiry) are both non-read-only.
  `memory_maintenance_status` is the only genuinely read-only console tool.
- **`memory_review_queue` carries `idempotentHint=True`** — the only write is
  the lazy expiry of already-overdue proposals, so re-listing lands on the same
  end state and a re-poll is safe. Core's `memory_review_queue` omits the hint
  (spec default: false); worth aligning core in a later lane-A/C packet.
- **`ge=1` on `k`/`limit` was deliberately NOT copied from core** (core
  `mcp_server.py` added it in WP13). WP14 is metadata-only; a schema minimum
  would start rejecting `k=0`, a real behavior change. The console's shared-tool
  schemas therefore differ from core's in validation. Defer to a separate packet
  if core/console schema parity is wanted.

**Dual-major evidence:** console suite green on both SDK majors —
`198 passed` on mcp 1.28.1 (`console/.venv`) and `198 passed` on a scratch venv
pinned to `mcp==2.0.0`. `_tool_manager.list_tools()` plus `Tool.parameters` /
`Tool.annotations` (the private FastMCP/MCPServer surface the contract test
reaches into) are unchanged across 1.x → 2.0, so `_mcp_compat` needs no new
accessor.

---

## WP15 — Entity name collation (`name_key`)

**Branch:** `wp15-entity-collation` · **Blocked by:** — (design:
`docs/superpowers/specs/2026-08-06-entity-case-collation-decision.md`, rev 2) ·
**Gate:** lane-A six-week read — **waived by the maintainer 2026-08-07**, with
the recommendation approved the same day · **Effort:** S · **Lane A — claims
`memory.py` + `store/` + `extract/`** · Sequenced **before WP4**, whose
`get_all(subject=…)` is the project's first public name-keyed read and must
inherit a settled policy rather than invent one.

**Goal:** One real-world subject resolves to one entity regardless of the casing
the user typed, so the WP11 restatement skip and contradiction resolution — both
keyed on `(subject_id, predicate)` — actually apply to it. Closes
[#14](https://github.com/Wuesteon/lean-memory/issues/14).

**Shipped:** option (c) of the decision doc. `entity.name_key` (NFC + casefold +
whitespace collapse) resolved on `(namespace, name_key, type)` with an
`ORDER BY created_at, id LIMIT 1` tie-break for legacy split rows; `entity.name`
keeps the first-seen surface form; schema v3 versioned migration (ALTER + Python
backfill + `ix_entity_key`, all inside the `< 3` branch, nothing in the
always-run blob); `normalize_text` promoted to `lean_memory/normalize.py` and
re-exported from `maintain/transforms.py`; `re.I` on the stub generator's
`_FIRST_PERSON`; `llm_typer` known-entity canonicalization on the shared key.
**Not** shipped, deliberately: healing pre-existing splits, object-side entity
resolution, alias/semantic linking, the `_CAP_RUN` lowercase-proper-noun
misextraction, console `name_key` adoption (separate package/release).

**Files:** `src/lean_memory/normalize.py` (new), `store/base.py` (contract
docstring), `types.py` (Entity docstring), `store/schema.py` (comments only — no
DDL change), `store/sqlite_store.py`, `maintain/transforms.py` (re-export),
`extract/gliner_extractor.py`, `extract/llm_typer.py`,
`tests/test_entity_collation.py` (new), `tests/fixtures/make_v2_fixture.py` +
`v2_format.db` (new), `tests/test_schema_migration.py`,
`tests/test_schema_version.py`, `tests/test_restatement_dedupe.py`,
`tests/test_phase1_extraction.py`, `README.md`, `CHANGELOG.md`.

**Decisions the design doc delegated to WP15:**

- **The stub's first-person pattern does NOT gain rules.py's `I am`
  alternative** (§3.1 left this open: add it for real parity, or state the
  divergence). It is stated, in a comment at the pattern: `I am` is
  *unreachable*. Alternation is leftmost-first, so wherever `I am` would match
  the earlier `I` alternative already matches (the `\b` after `I` holds before a
  space), and both patterns are only ever consumed as a boolean `.search()`.
  Verified over a 13-case probe including `"I am a doctor."`, `"i am tired"`,
  `"Iam"`, `"hI am"`: the three patterns (stub with `re.I`, stub + `I am`,
  rules.py) agree on every input. Adding it would advertise a behavioral
  difference that does not exist — the same cargo-culting that produced the
  `_norm`/`normalize_text` drift this packet is undoing. The only remaining
  divergence is the non-capturing group, which the boolean use does not need.
- **The shared key is exported as both `normalize_text` and an
  `entity_key` alias** from `lean_memory.normalize`. One function object, two
  names: call sites read as what they are (`entity_key(name)` in the store and
  typer, `normalize_text(fact_text)` in DEDUP-EXACT) while remaining incapable
  of drifting apart. `router.py`'s `_norm` stays local and unchanged — it is a
  coref heuristic, not an identity decision.
- **The replacement known limit is pinned WITH its recoverability.**
  `test_case_distinct_subjects_merge_known_limit` asserts the merge, the
  supersession chain (`is_latest=0` + `superseded_by`), *and* that the retired
  fact is still returned by `search(as_of=…, is_latest_only=False)` — so the
  doc's "a false merge is recoverable" argument is executable rather than
  rhetorical.
- **The versioned migrations now run inside an explicit `BEGIN IMMEDIATE`**
  (review carry-in, 2026-08-07). Python's `sqlite3` opens an implicit
  transaction for DML only, never for DDL, so the v3 `ALTER TABLE ... ADD COLUMN
  name_key` was autocommitted and durable *before* the backfill / index / stamp
  it depends on. A/B-verified by SIGKILLing between the ALTER and the commit:
  pre-fix the file kept `name_key` under `user_version = 2` and **every** later
  open raised `duplicate column name: name_key` — permanently unopenable;
  post-fix the ALTER rolls back and the next open migrates cleanly. The same
  probe run concurrently (two processes first-opening one v2 file) failed the
  same way pre-fix and passes post-fix, because the version is re-read *under*
  the write lock. The outer `< SCHEMA_VERSION` guard keeps the common
  already-current open lock-free. The v2 branch inherited the same latent hazard
  in a two-statement window and is now covered too.
- **`ix_entity_lookup(namespace, name, type)` is retained but vestigial** and
  now says so in `schema.py`. No engine read keys on `entity.name` after v3, so
  it is pure write cost; retiring it needs a `DROP INDEX` in the versioned
  branch *and* an edit to a `create`-bearing line, which flips the console's
  engine-schema tripwire — a cross-package call, deliberately not slipped in
  here. Revisit with the console's `name_key` adoption.
- **`schema.py`'s comment avoids DDL keywords.** The console's engine-schema
  tripwire (`inspect_sql.compute_engine_schema_fingerprint`) digests every line
  of `store/schema.py` containing `create`, case-insensitively — comments
  included. A first draft of the v3 note mentioned the statements by name and
  flipped that hash with the DDL byte-identical, reddening the console suite.
  Reworded rather than re-baselined, since WP15 must not touch console source.
  **Recorded for whoever owns the tripwire next:** it digests `schema.py` only,
  and the real v3 DDL lives in `sqlite_store.py`'s versioned branch — so the
  tripwire is simultaneously over-sensitive to prose and blind to the actual
  schema change. Worth revisiting when the console adopts `name_key`.

**Verification:** core `346 passed` (baseline `319`, minus the deleted WP11
known-limit pin, plus 28 new: 18 collation, 5 extractor, 5 migration — the last
of those being the review carry-in's atomicity pin,
`test_interrupted_migration_rolls_back_whole`, confirmed to redden against the
pre-fix `sqlite_store.py`); console `198 passed`, unchanged — the engine-schema
tripwire hash was re-measured byte-identical after each `schema.py` comment edit,
not assumed. The decision doc's §6.4 blast-radius prediction was
re-measured against the real implementation rather than trusted: the finished
`src/` over the **unmodified** test suite gives `313 passed, 6 failed` — the
exact six tests §6.4 names. `PRAGMA user_version` = 3 confirmed on a fresh
store, on the v2 fixture, and on the v1 fixture (which crosses both branches in
a single open).

---

## Open follow-ups (recorded, not yet packets)

- ~~**Entity case-collation policy** ([#14](https://github.com/Wuesteon/lean-memory/issues/14))~~ — **RESOLVED by WP15
  (2026-08-07)**, no longer an open follow-up. WP11's pinned known limit ("acme"
  vs "Acme" split the slot and bypass dedupe + contradiction resolution) needed
  a decision, not just code, because case-insensitive lookup has real
  counterexamples ("Polish"/"polish"). Decision doc recorded 2026-08-06
  (`docs/superpowers/specs/2026-08-06-entity-case-collation-decision.md`,
  adversarially reviewed rev 2); the maintainer approved its recommendation and
  waived the six-week gate on 2026-08-07. Shipped as WP15: stored
  `entity.name_key` casefold column + `re.I` on the extractor's first-person
  regex (the headline example was two independent defects). #14 closes when the
  branch merges; the *new* known limit (case-distinct subjects merge) is
  recorded in §WP15, not here — it is an accepted, pinned trade, not an open
  question.
- **WP2 mem0 comparison arm** ([#15](https://github.com/Wuesteon/lean-memory/issues/15)) — designed as Task 5 of the WP2 plan
  (`--arm mem0`, version-pinned output, exit-2 on missing install); needs the
  user's go-ahead plus a configured mem0 LLM path (Ollama or API key) to run.
- **WP9 LLM-judge tier** (tracked under [#18](https://github.com/Wuesteon/lean-memory/issues/18)) — design recorded
  (`docs/superpowers/specs/2026-07-29-wp9-llm-judge-design.md`), gated on the
  six-week read or the WP9 trigger.

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
