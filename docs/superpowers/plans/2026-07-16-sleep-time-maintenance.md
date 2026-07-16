# Sleep-Time Maintenance (WP10a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved sleep-time maintenance design
(`docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md`, rev 3):
an offline job that dedupes, summarizes-old, and evicts-low-value memory
between sessions while preserving the ADD-only spine and as-of semantics per
the spec's §3.1 theorem (three verbs + ingest commutation), with a staged
human review queue exposed over MCP (console UI follows as WP10b). Every
design decision is already made and verified — **do not re-derive**; when a
step below conflicts with the spec, the spec wins.

**Architecture:** All mutations flow through four sanctioned store verbs
(`supersede_fact` + duplicate-cascade, `retire_duplicate`, `set_tier`, append
via `add_fact`) inside short `batch()` transactions. Two tiny ingest hooks
(duplicate-cascade closure, summary-staleness cascade) restore ingest
commutation and are provable no-ops until maintenance has ever run. Proposals
live in the namespace `.db` (schema v2, user_version-gated migration); the
lease is a partial-unique-index INSERT. Transforms: DEDUP-EXACT (auto),
EVICT auto-band (auto), DEDUP-NEAR / SUMMARIZE / EVICT (proposals). Promotion
is explicit-only, permanently. No physical deletion anywhere.

**Tech Stack:** Python ≥3.10, sqlite-vec 0.1.9 (vec0 TEXT-metadata UPDATE +
KNN tier filter empirically verified — spec §14), FTS5, pytest offline with
FakeEmbedder/StubTyper, FastMCP (mcp 1.28.0, `@mcp.prompt()` verified),
Ollama behind `[llm]` only.

## Global Constraints

- Offline suite green at every commit: `.venv/bin/python -m pytest tests/ -q`.
  Console suite too when console files change:
  `console/.venv/bin/python -m pytest console/tests -q` — the console has its
  OWN venv; the root `.venv` cannot import `lean_memory_console`.
- ADD-only discipline: no DELETE of fact/vec0/FTS rows anywhere in this plan.
- Offline-by-default: extractive stub summarizer is the default; `[llm]` is
  opt-in; `LM_FORCE_STUBS` honored; no test touches the network.
- The two ingest hooks must be byte-identical no-ops on any DB where
  maintenance never ran (spec §10.10 pins this) — the launch first-run path
  is sacred.
- stdout hygiene: nothing in the MCP server process (or any child it spawns)
  may write to fd 1 except the JSON-RPC stream.
- Work on branch `wp10a-sleep-maintenance` off `main` (claim the packet in
  `workpackets.md`). Commit per task.
- Existing pins must stay green untouched: `tests/test_spine.py`,
  `tests/test_asof_sparse.py`, `tests/test_functional_slot_supersession.py`,
  `tests/test_search_now.py`.
- Task order matters: 1→2→3 are strictly sequential (plumbing → schema →
  hooks); 4–6 build on them; 7–8 are the surfaces; 9 closes out.

---

### Task 1: Store plumbing — busy_timeout, batch(), new verbs

Spec §4.0, §7.1. The foundation everything else stands on.

**Files:**
- Modify: `src/lean_memory/store/base.py`, `src/lean_memory/store/sqlite_store.py`
- New: `tests/test_store_maintenance_verbs.py`

**Interfaces:**
- `SqliteStore.__init__(..., busy_timeout_ms: int = 1500)`; `_connect` issues
  `PRAGMA busy_timeout`. Maintenance opens stores with `busy_timeout_ms=5000`.
- `batch()` context manager: `BEGIN IMMEDIATE`, suppresses per-call commits
  (a store-level flag checked by every `commit()` call site), single COMMIT
  at exit, ROLLBACK on exception. Uses `execute()` only — never
  `executescript()` (implicit-commit trap, verified).
- `retire_duplicate(loser_id, survivor_id)`: `is_latest=0` +
  `superseded_by=survivor` on fact + `fact_vec.is_latest=0`, `valid_to`
  untouched. Chain invariant (spec §4.0, rev-3 blocker fix), maintained two
  ways: (i) resolve the survivor arg to its live canonical at call time
  (depth 1); (ii) **re-point existing losers of the loser**
  (`UPDATE fact SET superseded_by=:survivor WHERE superseded_by=:loser AND
  valid_to IS NULL`) so every open retired duplicate always points directly
  at an `is_latest=1` row — without (ii), a B→A→D chain resurrects B when D
  is later superseded.
- `set_tier(fact_id, tier)`: `fact.tier` + `fact_vec.tier` in one txn.
- `get_embedding(fact_id) -> np.ndarray | None` (read back, never re-embed).
- `iter_latest_facts(after_id=None)`, `iter_slots_touched_since(cursor_id)`
  (new-facts-since-cursor → DISTINCT slots; slot transforms then read the
  full slot via `find_latest_in_slot`).

- [ ] **Step 1:** busy_timeout param + PRAGMA in `_connect`; default 1500 ms. Assert existing tests still green (the console `retry_busy` stacking budget is documented in the spec — no console change needed here).
- [ ] **Step 2:** `batch()` with commit-suppression; convert store mutators to route their commit through one helper so suppression is a one-place change.
- [ ] **Step 3:** the four new verbs + the two iterators, each following `supersede_fact`'s two-surface discipline.
- [ ] **Step 4:** tests — two-surface sync for `retire_duplicate`/`set_tier`; batch atomicity (raise mid-batch → nothing committed); `get_embedding` round-trip; `iter_slots_touched_since` finds a duplicate landing on a long-quiet slot (the verified cursor gap); chain invariant: retire B→A then A→D re-points B to D (zero open duplicates pointing at a non-latest row).

### Task 2: Schema v2 — versioned migration, ledger, proposals

Spec §5. First real persisted-format change; carries the WP6-flagged
migration obligation.

**Files:**
- Modify: `src/lean_memory/store/schema.py`, `src/lean_memory/store/base.py`, `src/lean_memory/store/sqlite_store.py` (`_init_schema` + ledger/proposal CRUD), `src/lean_memory/types.py` (`Fact.record_kind` default `'fact'`), `console/src/lean_memory_console/inspect_sql.py` (fingerprint)
- New: `tests/fixtures/v1_format.db` (checked-in, tiny, built by a script committed beside it), `tests/test_schema_migration.py`

**Interfaces:**
- `_init_schema` restructured: always-run `executescript(SCHEMA_SQL)` (all
  IF-NOT-EXISTS) **plus** a `if user_version < 2:` branch running the
  non-idempotent DDL (`ALTER TABLE fact ADD COLUMN record_kind ...`) exactly
  once, then stamping 2 — never lowering a newer stamp (verified trap:
  ALTER raises `duplicate column name` on reopen if left in the blob).
- New tables per spec §5 verbatim: `fact_derivation` (+
  `ix_derivation_source`), `maintenance_run` (+ partial unique index
  `ux_run_live ON maintenance_run(namespace) WHERE status='running'`),
  `maintenance_proposal` (incl. `expiry_reason`, `evidence_backend`).

- [ ] **Step 1:** DDL + versioned `_init_schema`; `record_kind` threaded through `Fact`, `add_fact`, `_row_to_fact`.
- [ ] **Step 2:** build + check in the v1-format fixture DB; migration test: opens, upgrades once, **reopens cleanly** (the ALTER-idempotence trap), round-trips a search.
- [ ] **Step 3:** update the console `EXPECTED_SCHEMA_FINGERPRINT` (the new CREATE TABLE lines trip it — verified); console suite green. This lands HERE, in WP10a, with the migration — spec §5; deferring it to WP10b merges this packet with a red console suite.
- [ ] **Step 4:** ledger/proposal CRUD on the store (spec §4.0 "+ ledger/proposal CRUD" — create-half needed by Task 5's runner, so it cannot wait for Task 6): `create_run` / `heartbeat_run` / `finish_run` / `get_live_run`, `stage_proposal` / `get_proposal` / `list_proposals(status=...)`. Pure row CRUD — no decide/apply logic (that's Task 6). Unit tests: round-trip + `ux_run_live` uniqueness (second live INSERT hits the constraint).

### Task 3: The two ingest hooks (commutation)

Spec §4.0 (duplicate-cascade), §4.3 (staleness cascade), §3.1 condition 3.
These fix the two empirically-demonstrated rev-1 wrong answers.

**Files:**
- Modify: `src/lean_memory/store/sqlite_store.py` (`supersede_fact`), `src/lean_memory/memory.py` (`_apply_supersession`)
- New: `tests/test_ingest_commutation.py`

- [ ] **Step 1:** duplicate-cascade in `supersede_fact`: after closing `old`, `UPDATE fact SET valid_to=? WHERE superseded_by=old.id AND valid_to IS NULL` (same world-time V). `supersede_fact` now RETURNS the full closed-id list — `[old.id]` + cascade-closed ids (spec §4.0, rev 3).
- [ ] **Step 2:** staleness cascade in `_apply_supersession`: feed the closed-id sets **returned by `supersede_fact`** (explicit targets PLUS cascade-closed duplicates — not just the loop's own targets) into a `fact_derivation` lookup via `ix_derivation_source`; any derived summary still `is_latest=1` gets `is_latest=0`, `valid_to=new.valid_at`, `invalidated_by=new.id` (+vec mirror).
- [ ] **Step 3:** the regression tests from spec §10.2/§10.3 — **resurrection** (dedup, then supersede the survivor; `as_of` after the supersession returns only the new fact) **plus the transitive variant** (retire B→A, then A→D, then supersede D: assert B was re-pointed and BOTH A and B closed at V), and **stale summary**. No summarize implementation exists until Tasks 4/6, so the stale-summary test **hand-inserts its fixture**: an `add_fact` summary row (`record_kind='summary'`, `predicate='summary'`, `is_inference=1`) plus a direct `fact_derivation` INSERT (schema exists since Task 2); then contradict a source via ordinary ingest and assert the summary actually flipped (`is_latest=0`, `valid_to=new.valid_at`, `invalidated_by` set). Assert `fact_derivation` is non-empty FIRST, so the test can never pass vacuously.
- [ ] **Step 4:** the no-op pin (spec §10.10): full ingest+search byte-equivalence on a DB where maintenance never ran.

### Task 4: MaintenanceConfig, scoring, transforms

Spec §3.6, §4.1–§4.4. The heart of the job — pure functions over the Store
ABC.

**Files:**
- New: `src/lean_memory/maintain/__init__.py`, `maintain/config.py`, `maintain/score.py`, `maintain/summarize.py`, `maintain/transforms.py`
- New: `tests/test_maintenance_transforms.py`, `tests/test_maintenance_asof_grid.py`

**Interfaces:**
- `MaintenanceConfig` frozen dataclass with the spec §3.6 defaults +
  `config_hash()` (canonical JSON → sha256).
- `score.value(fact, now)` — spec §4.4 formula; recency anchor is VERBATIM
  the retriever's: `(last_access or valid_at)` (`retriever.py:97`) — not
  `created_at`, and not `max(last_access, valid_at)` (the `max` form diverges
  on future-dated facts accessed before their `valid_at`).
- `Summarizer` protocol; `ExtractiveStubSummarizer` (deterministic,
  top-salience fact_texts); `OllamaSummarizer` behind `[llm]` import guard.
- Transforms as pure functions `(store, config, embedder, summarizer) →
  actions/proposals`: `dedup_exact` (auto; value-preserving normalization =
  NFC + casefold + whitespace collapse ONLY; survivor argmin(valid_at),
  tiebreak min id; last_access merge = max over cluster of
  coalesce(last_access, valid_at); access_count summed), `dedup_near`
  (propose; τ≥0.95 on stored embeddings via `get_embedding`; multivalued
  flag in payload), `summarize` (propose; per spec §4.3 steps, embedding
  computed BEFORE the batch window), `evict` (auto strict band + propose;
  guards per §4.4; intra-run ordering: stage all proposals over the
  pre-transform snapshot, then auto-apply excluding staged-proposal
  targets).

- [ ] **Step 1:** config + hash + score (unit tests incl. backfill anchor case).
- [ ] **Step 2:** summarizer seam + stub (deterministic output pinned by test); Ollama variant import-guarded.
- [ ] **Step 3:** the four transforms with the guards and ordering above.
- [ ] **Step 4:** the as-of grid test at the **store predicate** (spec §10.1), for what is executable at this task — the AUTO transforms (dedup_exact, evict auto-band) plus staging itself (which must produce a ZERO spine delta): ids satisfying the visibility predicate over a T grid, `is_latest_only=False`, identical for all T < t_m; intended deltas at T ≥ t_m. The propose-transforms' spine effects exist only after the Task-6 apply path — Task 6 Step 4 re-runs this grid post-apply (spec §10.1 rev-3 note). Plus: no inverted intervals; multivalued never auto-merged; ranking-delta pin after DEDUP-EXACT (spec §10.8).

### Task 5: Runner — lease, cursor, thresholds, ledger

Spec §6 (thresholds, cursor semantics), §7.2–§7.4.

**Files:**
- New: `src/lean_memory/maintain/runner.py`
- New: `tests/test_maintenance_runner.py`

- [ ] **Step 1:** lease claim: BEGIN IMMEDIATE → check live-heartbeat row → INSERT run row (loser hits `ux_run_live` constraint) → COMMIT; heartbeat at every batch commit and ≥ every 30 s; stale threshold `max(5 min, 10× longest observed batch)`; stale takeover marks `'aborted'`.
- [ ] **Step 2:** work thresholds (≥200 facts since cursor OR cumulative new-fact salience ≥300 OR ≥7 days) — below them the run is a clean no-op (spec §10.9).
- [ ] **Step 3:** cursor: advance before writing outputs; candidate scans exclude `record_kind='summary'` and maintenance episodes; slot transforms driven by `iter_slots_touched_since`.
- [ ] **Step 4:** crash/resume test (kill between batches → consistent DB, lease takeover, convergent re-run, no double-summary); two-process lease race test (second runner cleanly skips).

### Task 6: Proposal lifecycle + Memory façade + retrieval changes

Spec §5 (CAS + apply re-validation), §8.

**Files:**
- Modify: `src/lean_memory/memory.py`, `src/lean_memory/store/sqlite_store.py` (tier filters), `src/lean_memory/retrieve/retriever.py` (thread `include_cold`)
- New: `tests/test_proposal_lifecycle.py`, `tests/test_tier_retrieval.py`

- [ ] **Step 1:** CAS decide + apply, on top of the Task-2 CRUD, exactly per spec §5; apply = one `batch()`: CAS → **re-validate targets** (all still `is_latest=1`, dedup pairs still co-valid; stale ⇒ `status='expired', expiry_reason='stale_target'`, spine untouched) → verbs → `applied_at`. Re-apply retry returns "already applied".
- [ ] **Step 2:** `Memory.maintain(config=...)`, `review_queue()`, `decide()`, `promote()` (explicit-only — no auto-promotion anywhere), `search(..., include_cold=False)`. `maintain()`/apply open a **dedicated maintenance `SqliteStore`** on the namespace file with `busy_timeout_ms=5000` for the run, closed at run end — the serving store stays at 1500 (spec §7.1; this is how the 5000 budget reaches the in-process MCP path).
- [ ] **Step 3:** tier filter: dense `AND tier='hot'` (vec0 metadata, verified), sparse in the per-row recheck; applied only in default latest-mode; `as_of` NEVER filters tier. Byte-identical default when nothing is cold (regression pin).
- [ ] **Step 4:** tests — lifecycle incl. stale-target expiry + full-DB-hash reject invariance (spec §10.7); `as_of × include_cold × tier` matrix + arm agreement (spec §10.6); edited-approve records human provenance and re-scores with `source='user'`; **apply-path as-of grid re-run** (spec §10.1 — the headline claim): approve one summarize + one dedup-near + one evict proposal, then re-assert predicate invariance for all T < t_a and the intended deltas at T ≥ t_a — the only place the propose-transforms' spine effects can be grid-pinned.

### Task 7: CLI + memory_clear lease-refusal

Spec §6.1, §7.3.

**Files:**
- New: `src/lean_memory/maintain/cli.py`
- Modify: `pyproject.toml` (`lean-memory-maintain = "lean_memory.maintain.cli:main"`), `src/lean_memory/mcp_server.py` (`memory_clear`)
- New: `tests/test_maintenance_cli.py`

- [ ] **Step 1:** `lean-memory-maintain --root [--namespace] [--apply] [--auto-only] [--json]`; **dry-run default**; per-namespace report to stdout (own process — stdout free); `--json` for machines.
- [ ] **Step 2:** `memory_clear` refuses with an explanatory message while a live-heartbeat lease exists; maintenance skips cleared namespaces at the next batch boundary; the residual sliver documented in the tool docstring (spec §7.3 honest statement).
- [ ] **Step 3:** cross-process test: CLI (subprocess) vs a live store writer interleave without unhandled `database is locked` (busy_timeout + lease).

### Task 8: MCP surfaces — tools ×3, prompt, plugin command, auto-spawn

Spec §6.3–§6.5, §1.3.10. The v0.1.3 lesson lives here; be exact.

**Files:**
- Modify: `src/lean_memory/mcp_server.py`, `console/src/lean_memory_console/observe_mcp.py`, `console/src/lean_memory_console/routes/mcp.py`, `console/src/lean_memory_console/engine.py` (4 `EngineGateway` maintenance methods — boundary corrected from WP10b: both console MCP surfaces write only through the gateway, so the v1 tools need them), `plugin/.mcp.json` + `server.json` (reconcile), `tests/test_stdout_hygiene.py`, console tests
- New: `plugin/commands/review-memory.md`, `tests/test_mcp_maintenance_tools.py`

- [ ] **Step 1:** the four tools — `memory_maintenance_run(namespace, apply=False)` (**dry-run default**, symmetric with CLI), `memory_maintenance_status` (must not trigger the lazy model build — reads the DB only; pin with a test), `memory_review_queue(namespace, kind=None, limit=20)` (grouped by entity, evidence inline), `memory_review_decide(namespace, proposal_id, decision, edited_text=None)` (approve|reject|edit|promote) — registered on **all three** surfaces: core stdio, console `observe_mcp.py` (what the plugin ships), console HTTP mount. The console surfaces reach the engine ONLY through `EngineGateway` (spec §1.3.8), so this step also adds the four gateway methods (`maintain`, `review_queue`, `decide`, `promote`), each wrapping `retry_busy` + the per-namespace asyncio lock + the single worker thread exactly like `add`/`search` (spec §8). In the same change, update the exact-set pin `console/tests/test_mcp_parity.py::test_wrapper_exposes_exactly_add_and_search` to the new six-tool set — it asserts set equality and goes red the moment the wrapper grows.
- [ ] **Step 2:** `@mcp.prompt() review-memory-maintenance` on the console stdio server + the plugin command file `plugin/commands/review-memory.md` (the client-portable path). Prompt text: fetch queue → present batched by entity/kind with evidence → collect explicit user verdicts → decide per item → summarize; **forbids the client agent deciding without an explicit user verdict**; batch verbs only on explicit user statements.
- [ ] **Step 3:** auto-spawn (`LM_MAINT_AUTO=1`, default OFF), inside `_mem()` on first tool call: indexed staleness read → `Popen([...,'--apply','--auto-only'], stdin=DEVNULL, stdout=DEVNULL, stderr=<log or DEVNULL>, start_new_session=True, close_fds=True)`. fd 1 must never be inherited.
- [ ] **Step 4:** extend the stdout-hygiene test to cover tool calls with `LM_MAINT_AUTO=1` (parent JSON-RPC stream byte-clean while the child runs); manifest reconciliation checked by the existing server-manifest test (extend `tests/test_server_manifest.py` if it doesn't already cover the tool list).

### Task 9: Docs + close-out

**Files:**
- Modify: `README.md` (a "Sleep-time maintenance & review" section: the CLI, the cron recipe, the Claude Code review workflow, the safety story in one paragraph), `CHANGELOG.md`, `ARCHITECTURE.md` (status table row), `docs/superpowers/workpackets.md` (WP10a → merged status when done)

- [ ] **Step 1:** README section — lead with the user story ("overnight cleanup, next-morning click-through in Claude Code"), then the invariants (nothing deleted; history queryable as-of any past time; unreviewed proposals expire).
- [ ] **Step 2:** CHANGELOG entry; ARCHITECTURE row; full offline suite + console suite green; whole-branch review per house flow; update the packet status table.

---

## Explicitly out of scope (do not drift into these)

- WP10b console Review page (own packet, starts after this merges).
- Physical space reclamation, episode compaction, deletion of any kind (WP5).
- Automatic promote-on-access (decided against permanently, spec §4.4/§12).
- Any change to `bench/` or the frozen calibration constants.
