# Maintenance Review UI (WP10b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The next-morning click-through (spec
`docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md` §8.1):
a console `Review` page listing pending maintenance proposals grouped by
entity with before/after evidence and the verbs approve / keep (reject) /
edit-then-approve / promote, plus batch approve per group — served by new
console routes that reach the engine ONLY through the `EngineGateway`
maintenance methods WP10a already shipped. Also closes the four WP10a
carry-in cleanups recorded in `workpackets.md`.

**Architecture:** One new FastAPI router (`routes/review.py`, mirroring
`build_views_router`'s late-import + `Depends(require_auth)` pattern) proxies
five gateway calls: `review_queue`, `decide`, `promote`,
`maintenance_status`, `maintain`. **Deliberate deviation from spec §8.1's
"reads via inspect_sql" sketch:** WP10a implemented `review_queue` as a
write-ish operation (it lazily expires overdue proposals,
`memory.py:346-371`), so queue reads MUST go through the gateway's
lock+worker path — one code path, no double-implemented expiry. The UI adds
one page (`Review.tsx`) wired into the existing `App.tsx` routes +
`Layout.tsx` nav, calling new typed helpers in `api.ts`. The gateway's
`review_queue` already returns `[{entity_id, entity_name, proposals:[...]}]`
with parsed payloads — the page renders it directly.

**Tech Stack:** FastAPI (console venv: `console/.venv`), React 18 +
react-router 6 + Tailwind (ui/, no test runner — `npm run typecheck` +
`npm run build` are the gates; built static is NOT committed, vite outDir
targets `console/.../static/` at build time).

## Global Constraints

- Core suite green: `.venv/bin/python -m pytest tests/ -q`. Console suite
  green: `console/.venv/bin/python -m pytest console/tests -q` (own venv —
  the root venv cannot import `lean_memory_console`).
- All review mutations go through `EngineGateway` — no raw SQL writes, no
  direct `Memory` construction in routes.
- Reserved namespaces (`_*`) rejected exactly like the views router
  (`_ns_db` pattern, `routes/views.py:17-24`).
- Auth-gated: every new route carries `Depends(require_auth)`.
- ADD-only discipline untouched: this packet writes no engine transform
  logic; carry-ins are behavior-preserving cleanups except where noted.
- Branch `wp10b-review-ui`; commit per task; claim recorded in
  `workpackets.md`.

---

### Task 1: WP10a carry-in cleanups (core)

The four items recorded in `workpackets.md` from WP10a's final review.

**Files:** `src/lean_memory/maintain/transforms.py`,
`src/lean_memory/maintain/cli.py`, `src/lean_memory/store/base.py`,
`src/lean_memory/maintain/mcp_support.py`; tests beside the existing ones.

- [ ] **Step 1:** `transforms.summarize`: move the proposal-budget check
  ahead of the summarizer invocation (today the summary text is generated
  before the budget truncates — wasteful once Ollama is the `[llm]`
  summarizer). Pin with a test: a budget of 0 must invoke the summarizer
  zero times (spy summarizer).
- [ ] **Step 2:** CLI exists-guard: `lean-memory-maintain --namespace NS`
  against a nonexistent `<NS>.db` must error cleanly (exit 2, message)
  without creating an empty DB file. Test: dry-run against a missing
  namespace leaves the root directory unchanged.
- [ ] **Step 3:** declare `path` on the `Store` ABC (it is accessed
  polymorphically; today only `SqliteStore` defines it).
- [ ] **Step 4:** document the asymmetry in the `memory_maintenance_run`
  tool docstring: `apply=True` runs the auto band AND stages proposals,
  while auto-spawn `--auto-only` runs the auto band only.

### Task 2: Console review routes

**Files:** new `console/src/lean_memory_console/routes/review.py`; modify
`console/src/lean_memory_console/app.py` (register router); new
`console/tests/test_review_views.py`.

**Interfaces (all auth-gated, all reserved-namespace-guarded, all
JSON):**
- `GET  /views/{ns}/review/queue?kind=&limit=` → gateway.review_queue
  (entity-grouped payloads, verbatim).
- `POST /views/{ns}/review/{proposal_id}/decide` body
  `{decision: approve|reject|edit, edited_text?}` → gateway.decide; the
  gateway/lifecycle CAS answer ("already decided/applied") passes through
  as a 409-with-body, not an exception.
- `POST /views/{ns}/review/promote` body `{fact_id}` → gateway.promote.
- `GET  /views/{ns}/maintenance/status` → gateway.maintenance_status.
- `POST /views/{ns}/maintenance/run` body `{apply: bool=false}` →
  gateway.maintain (dry-run default, symmetric with CLI/MCP).

- [ ] **Step 1:** router file mirroring views' build pattern (late
  `require_auth` import, prefix `/views`), registered in `app.py` BEFORE
  the static catch-all.
- [ ] **Step 2:** tests (pattern: `console/tests/test_views.py` +
  `test_engine.py` fixtures): queue round-trip against a real staged
  proposal (use the engine to stage via `maintain(apply=True)` on a
  seeded namespace); decide approve/reject/edit round-trips incl. the
  double-decide 409; promote flips tier; status shape; run dry-run
  default stages nothing; auth 401 without token; reserved ns 404.

### Task 3: Review page (UI)

**Files:** new `ui/src/pages/Review.tsx`; modify `ui/src/App.tsx`,
`ui/src/components/Layout.tsx` (nav item), `ui/src/api.ts`,
`ui/src/types.ts`.

- [ ] **Step 1:** types (`ProposalGroup`, `Proposal`, `MaintenanceStatus`)
  + five `api.ts` helpers through the shared `req<T>()` wrapper.
- [ ] **Step 2:** `Review.tsx`: status header (last run, pending count,
  expiring-soon; "Run maintenance (dry-run)" button + apply variant behind
  a confirm); groups by entity (collapsible), per-proposal card showing
  kind badge, evidence (before/after fact texts for dedup, source texts +
  proposed summary for summarize, score evidence for evict — GitHub
  suggested-changes style), verbs Approve / Keep / Edit-then-approve
  (textarea prefilled with proposed text) / Promote where applicable;
  **batch approve per group**; optimistic row removal on decision with
  409-refresh ("decided elsewhere" toast per spec §8.1 CAS surfacing).
  Follow existing page patterns (`Memories.tsx` load-effect guard,
  `TestSearchBox` submit-state, `FactDrawer` drawer layout).
- [ ] **Step 3:** nav entry `{to: "/review", label: "Review"}` +
  `<Route path="/review" .../>`; `npm run typecheck` and `npm run build`
  pass.

### Task 4: Close-out

- [ ] **Step 1:** README console section: one paragraph + screenshot-less
  description of the review flow; CHANGELOG under `[Unreleased]`.
- [ ] **Step 2:** both suites + typecheck green; workpackets WP10b row →
  merged status at merge time; whole-branch review before merge.

## Explicitly out of scope

- Any engine transform/lifecycle logic change beyond carry-in 1.
- Committing built static assets; screenshots; e2e browser tests.
- WP1 launch items (tag, listings, plugin version bump — noted for tag
  time).
