# lean-memory-console — agent-first memory verification console (design v2)

Date: 2026-07-10 · Status: approved design v2 (v1 platform design rethought
after external strategy review; v1's multi-tenant service preserved as Tier 2;
adversarial self-review of v2 applied — see §0)
Packet: **Memory UI** (workpackets.md status table) · Branch: `worktree-memory-ui` · Lane: **D**

## 0. What changed from v1 and why

v1 designed a multi-tenant platform (tenant registry, per-tenant API keys,
admin token, control-plane DB, six-page SPA) for a team-infrastructure buyer
the strategy explicitly defers. The actual v1 user is one human whose own
Claude Code writes memories and who opens a UI to verify them. The external
strategy review (2026-07-10 engagement readout) recommended exactly this
rescope: a single-tenant read-only inspector first, the multi-tenant control
plane only on demonstrated team demand. v2 therefore:

- **Rescopes Tier 1** to a single-tenant console with two deployment modes:
  a transient local viewer (think `jupyter notebook`, not Grafana) and a
  single-tenant Docker container (founder decision: containerized-from-day-one
  stays in Tier 1).
- **Adds the observing MCP wrapper** — observability without a mandatory
  server in the data path.
- **Adds the Claude Code plugin** as the primary distribution artifact.
- **Defers to Tier 2** (§14): tenant registry, per-tenant API keys, admin
  control plane — v1 §5/§7 designs carry over intact when team demand shows.
- **Cuts** the standalone metrics dashboard page (readout DL-4 resolution:
  microscope, not Grafana — headline numbers fold into Overview).
- **Absorbs the readout's spec deltas**: launch-window separation contract
  (§3), dead-recency banner deleted (the fix lands with WP0; the packet
  rebases onto post-WP0 main), fail-loud schema tripwire instead of a comment
  (§13).

v2 self-review corrections (adversarial review, 2026-07-10): the engine sets
**no `busy_timeout`** (`sqlite_store.py` `_connect()` sets only
`journal_mode=WAL` + `foreign_keys=ON`), so all cross-process write-safety is
console-owned (§6); the earlier "`mode=ro` fails with error 14 on
sidecar-less files" claim **failed empirical reproduction** and the open
strategy is now error-driven, not heuristic (§7); the "read-only console"
claim is qualified (§1); a dozen implementability gaps pinned throughout.

## 1. Goal

**Agents write and search; the human verifies.** The agent (Claude Code via
MCP, or any HTTP client in Docker mode) is the only writer of memory content.
The human opens the console **read-only over stored memory content** — no
adding, editing, or deleting facts. The single exception is the manual
test-search box (§7), which runs a real engine search and therefore bumps
access stats (`touch()`); that is observability of live-search behavior, not
memory mutation. The console makes the engine's invisible signals — the
ADD-only supersession spine, per-hit score decomposition, provenance
episodes — visible for the first time.

**Namespaces replace tenants.** The engine's one-SQLite-file-per-namespace
model already provides the isolation story: one namespace per project/agent.
Tier 1 has no tenant machinery at all.

## 2. Non-goals (Tier 1)

- No multi-tenancy: no tenant registry, no per-tenant API keys, no admin
  control plane (all Tier 2, §14).
- No memory editing or deleting from the UI (ADD-only discipline; deletion is
  WP5's design problem). Namespace deletion is also out — delete the file,
  documented, until WP5.
- No changes to the core library, its MCP stdio server, its tests, its
  pyproject, or `bench/` (lane-D rule, §3).
- No user accounts / RBAC / webhooks / usage metering.
- No SSE/websockets; the activity view polls (3–5 s).
- No standalone metrics/latency dashboard page (see §0).
- No component-level frontend tests (typecheck + production build gate).

## 3. Constraints and strategy context

- **Lane D file discipline.** No changes under `src/lean_memory/`, `bench/`,
  or lane-C files. The console runs its own read-only SQL for enumeration
  (test-search uses the engine, not raw SQL) until WP4's
  `get/get_all/history/explain` API lands (§13).
- **Launch-window separation contract** (from the engagement readout,
  CEO/CTO-committed): no Docker/server artifact lands on the core repo's
  default branch or is linked from it until the six-week post-launch read
  opens. This packet develops in the `worktree-memory-ui` worktree; at
  publication time the console moves to a separate public repo
  (`lean-memory-console`) which doubles as its plugin marketplace (§9).
  Founder decision D1 (separate repo vs. branch) remains open; the spec is
  laid out so either works.
- **Rebase gate:** no `console/` code is authored until this packet rebases
  onto post-WP0 main (frozen escalation constants, recency fix). Consequence:
  the v1 "dead recency" honesty banner is deleted from this design — on the
  merge target the recency term works; a false honesty banner is the opposite
  of trust positioning.
- **Anti-goals reconciliation:** the core library gains no mandatory
  server/daemon, no new dependency, no changed default (WP8 rule satisfied).
  The local console mode is a transient localhost process, not a daemon.
- **Relationship to WP8a/WP8b/WP8c:** the plugin (§9) is the future home of
  WP8a auto-capture hooks; Docker mode's REST surface previews WP8b and its
  pagination envelope (§7) is the contract WP8c's TS client targets.
- **Global invariants inherited:** offline suite green at every commit;
  ADD-only; offline-by-default (console runs fully on stub backends);
  Apache-2.0.

## 4. Architecture — one app, two modes

```
LOCAL MODE (default; zero Docker, nothing runs when the agent doesn't)
  Claude Code ──stdio──► lean-memory-console mcp   (observing MCP wrapper)
                             │  imports Memory; writes <root>/<ns>.db
                             │  + appends search traces to _events.db sidecar
  Human ──browser──► lean-memory-console serve     (transient, 127.0.0.1,
                             read-only over the same data root)

DOCKER MODE (single-tenant, long-running, container owns /data)
  Agents ──HTTP(MCP)/REST──► container: same FastAPI app
                             (data plane + console UI + event recording)
  Human ──browser──► same container, same LM_API_KEY
```

Components (satellite project; nothing in the core package):

```
console/                       # Python package `lean_memory_console`
  pyproject.toml               # deps: lean-memory (path/PyPI), fastapi,
                               # uvicorn, mcp; extras: [models] passthrough
  src/lean_memory_console/
    cli.py                     # `lean-memory-console serve|mcp` entry point
                               # + `--print-compose-path` (§9/§10)
    config.py                  # env/flag parsing, data-root resolution (§10),
                               # _SAFE_NS mirror + reserved-ns guard (§5)
    app.py                     # FastAPI factory (both modes), static mount
    engine.py                  # Memory instance pool per namespace,
                               # per-namespace asyncio write locks (intra-
                               # process), SQLITE_BUSY retry wrapper (§6)
    events.py                  # _events.db sidecar: schema, recording,
                               # supersession detection, atomic retention
    observe_mcp.py             # stdio MCP server (observing wrapper)
    inspect_sql.py             # read-only enumeration SQL over engine DBs
    routes/ (mcp.py, data.py, views.py)
    static/                    # built SPA (gitignored; built by ui/)
  tests/                       # offline pytest suite
    fixtures/build_fixture.py  # deterministic fixture builder (§12)
  README.md                    # quickstart, connect snippets
ui/                            # React 18 + TS SPA; Bun + Vite,
                               # react-router, Tailwind CSS v4, Recharts
plugin/                        # Claude Code plugin (§9); on extraction the
  .claude-plugin/plugin.json   # repo gains .claude-plugin/marketplace.json
  .mcp.json                    # (marketplace root = dir containing
  commands/  skills/           #  .claude-plugin/; source: "./plugin")
deploy/
  Dockerfile                   # multi-stage; named targets `slim`/`full` (§10)
  docker-compose.yml           # single service, /data volume, target: full
                               # (single source of truth — §9)
```

## 5. Storage: engine DBs + events sidecar

The console adds exactly one file to the data root: **`_events.db`** (SQLite,
WAL, opened with `PRAGMA busy_timeout=5000` — console-owned connections set
this explicitly because the engine does not, §6). Everything else is the
engine's own `<safe_namespace>.db` files.

**Reserved-namespace guard (console-owned).** The engine's sanitizer is the
private regex `_SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")` plus an
`… or "default"` fallback (`memory.py:38,70-71`); it *preserves* leading
underscores, so nothing in the engine stops a namespace named `_events`
colliding with the sidecar. Therefore: (a) namespace discovery globs `*.db`
and **skips `_*.db`**; (b) the observing MCP and Docker data plane **reject**
any namespace whose sanitized form is empty or begins with `_`. The console
mirrors the regex + fallback in one place (`config.py`), guarded by the
fail-loud tripwire (§13).

```sql
-- _events.db
event(id INTEGER PK, namespace TEXT, ts INTEGER,
      kind TEXT CHECK(kind IN ('add','search')),
      duration_ms REAL, payload TEXT);          -- JSON
CREATE INDEX ix_event_ns_ts ON event(namespace, ts);
```

Event payloads (identical in both modes — recorded by `observe_mcp.py`
locally and by the FastAPI handlers in Docker):

- `add`: `{episode_text_chars, source, t_ref, fact_ids, fact_count,
  superseded_fact_ids, superseded_count}`.
  **Supersession detection (pinned):** immediately after `Memory.add()`
  returns — while still holding that namespace's intra-process write lock —
  run `SELECT id FROM fact WHERE superseded_by IN (<returned fact ids>)` on
  the namespace DB (`superseded_by` points from the retired fact to the new
  one, so the IN-clause on *my* returned ids is inherently scoped to *my*
  add's supersessions even if another process writes concurrently).
  Cross-process caveat: the engine's `add` commits `add_fact` and
  `supersede_fact` separately (two commits, not one transaction), so a
  concurrent writer in another process can force `SQLITE_BUSY` mid-add — the
  §6 retry contract covers it. Stopgap until WP4 exposes supersession in the
  return value.
- `search`: `{query, k, latest_only, origin, hits: [{fact_id, fact_text,
  final_score, relevance, recency, importance, dense_rank, sparse_rank,
  rrf_score}]}`. Field sourcing: `fact_id`/`fact_text` come from the nested
  `RetrievedFact.fact` (`.fact.id`, `.fact.fact_text`); the seven score
  fields are copied directly from the `RetrievedFact` top level. `origin` ∈
  `agent|ui` (`ui` = the console's test-search, §7).

**Event-recording failure contract:** recording an event must never mask the
operation's own result — if the event INSERT itself fails (e.g. lock timeout
exhausted), it degrades to a log line, not an error response. Failed engine
calls keep their `add`/`search` kind and are marked solely by
`payload.error`; the UI renders an error badge from it.

**Graceful degradation:** adds are reconstructible from the engine DB alone
(episodes + facts + `ingested_at`), so a data root written by the *core*
stdio MCP server (no sidecar) still renders everything except search traces;
the Traces page then shows a "connect via the observing MCP to capture
traces" hint instead of an empty table.

**Naming note:** the wire parameter is `latest_only` everywhere (tool arg,
REST body, views query param, event payload). It maps to the engine kwarg
`is_latest_only` and filters the `fact.is_latest` column. Default `true`.

**Retention (atomic, cross-process-safe):** hard cap of 10k events per
namespace. After each INSERT, a cheap `COUNT` guard decides whether to prune;
pruning is one statement in the same connection —
`DELETE FROM event WHERE namespace=? AND id NOT IN (SELECT id FROM event
WHERE namespace=? ORDER BY ts DESC, id DESC LIMIT 10000)` — so interleaved
writers cannot over-prune. Overview surfaces the earliest stored event `ts`
so truncation is visible, never silent.

## 6. Write path (agent-facing)

**Cross-process concurrency contract (console-owned).** Verified: the engine
sets **no** `busy_timeout` (`sqlite_store.py` `_connect()` configures only
WAL + foreign keys; SQLite's default timeout is 0 ms), so a second concurrent
writer *raises* `SQLITE_BUSY` immediately instead of waiting. The console
cannot change the engine (lane D), so it owns the handling: (a) console-owned
connections (`_events.db`, read connections) set `PRAGMA busy_timeout=5000`
at open; (b) every engine write call (`Memory.add`, `touch()` via search) is
wrapped in a bounded retry-on-`SQLITE_BUSY` loop (3 attempts, short backoff);
(c) two processes writing one namespace concurrently (e.g. two Claude Code
sessions spawning the wrapper on the same data root) is a
supported-but-serialized-by-retry path, not lock-free — the README recommends
one namespace per project/session. Adding `busy_timeout` to the engine's
`_connect()` is recorded as engine-coupling debt for lane A (§13).

**Local mode — observing MCP (primary).** `lean-memory-console mcp` — stdio,
spawned by Claude Code (via the plugin or `claude mcp add`). Tools:

- `memory_add(namespace: str, text: str, source: str = "user",
  t_ref: int | None = None) -> {fact_ids, superseded_count}`
- `memory_search(namespace: str, query: str, k: int = 5)
  -> {hits: [{fact_text, final_score}]}` — always `latest_only=true`
  (the flag is REST-only; keeps the agent surface minimal).

The wrapper is a deliberate **superset** of the core stdio server's tools:
core `memory_add(namespace, text) -> str` gains `source`/`t_ref` and a
structured return; core's `memory_clear` is intentionally absent (no
deletion surface). Parity is with the underlying `Memory` API, not the core
tool signatures — see the §12 parity test for exactly what is pinned.

Namespaces are created implicitly: the engine's `_store()` lazily creates
`<safe_ns>.db` on first access, so the first accepted `memory_add` (or even a
search) materializes the file. There is no create-namespace endpoint; the
reserved-namespace guard (§5) applies to both paths.

`t_ref` (epoch-ms) is the world/event time that becomes `valid_at` and
anchors the temporal spine. Live agents omit it (wrapper fills `now`);
replay/import supplies it — omitting it on historical data silently collapses
the spine's ordering, so the README documents both modes.

**Docker mode — HTTP data plane.** Same tool vocabulary over streamable-HTTP
MCP at `/mcp` (Python `mcp` SDK / FastMCP mounted into FastAPI), plus a REST
mirror for non-MCP agents:

- `POST /v1/{namespace}/memories {text, source?, t_ref?}`
  → `{fact_ids, superseded_count}`
- `POST /v1/{namespace}/search {query, k?, latest_only?}` → full hit objects
  with score breakdown (richer than the MCP tool on purpose).

Connect snippet (rendered on the console's Overview page in Docker mode):

```bash
claude mcp add --transport http lean-memory http://<host>:8377/mcp \
  --header "Authorization: Bearer $LM_API_KEY"
```

Both modes: acquire the namespace's intra-process write lock (adds), call the
engine under the retry contract, time it, record the event, return.
`Memory.search`'s access-stat bump (`touch()`) is correct live-usage
behavior, not a defect.

## 7. Read path (human-facing console API)

**Auth by mode.** Local mode: bind 127.0.0.1 only; a random per-launch
session token is embedded in the auto-opened URL (`?token=…`) and required on
every request. Token hygiene: all responses set `Referrer-Policy:
no-referrer` (so the tokened URL cannot leak via Referer if memory content
ever contains an external link the user clicks); the SPA strips `?token` from
the address bar via `history.replaceState` on boot and holds it in React
context; the server validates the `Host` header is `127.0.0.1`/`localhost`
(DNS-rebinding belt-and-suspenders — the token is the second factor, not the
only one). The token dies with the process. Docker mode: `Authorization:
Bearer <LM_API_KEY>` on everything; the SPA login screen keeps the key in
React context — never localStorage, never a cookie; a page reload re-prompts
(accepted for v1). Single-tenant means one trust domain; plane-scoping
returns with multi-tenancy in Tier 2.

**Mode detection + auth probe — `GET /views/whoami`** (both modes) →
`{mode: "local"|"docker", auth: "token"|"bearer", authenticated: bool,
data_root: str}`. The `mode`/`auth` fields are readable without credentials;
`authenticated` reflects the presented credential, and all other endpoints
401 without a valid one. The SPA calls it on load: docker + 401 → login
screen; local + 401 → plain error (local has no login screen).

**Read-only SQL connections (error-driven, not heuristic).** Enumeration
endpoints open engine DBs with `file:<path>?mode=ro` — **always**, in both
modes; `mode=ro` reads WAL and non-WAL files, checkpointed or not, with or
without sidecars (empirically verified during self-review; the earlier
"error 14 without sidecars" claim did not reproduce). `immutable=1` is used
**only** as a per-request, short-lived fallback when the `mode=ro` open
actually raises `SQLITE_CANTOPEN` (error 14 — genuinely read-only media),
and is documented as a best-effort snapshot; it is never used while a writer
may exist, because `immutable=1` on a changing file is undefined behavior.

**test-search is the one write-path exception:** it runs through a
short-lived **writable** `Memory` instance from the engine pool (read-only
connections cannot execute `touch()`'s UPDATE), acquiring the namespace's
intra-process write lock. On `SQLITE_BUSY` (cross-process contention with a
live wrapper) the search still returns hits; only the `touch()` stat-bump is
best-effort — a failed bump is logged and swallowed, never surfaced as an
error.

**Pagination envelope (list endpoints; the WP8c contract):** `page` 1-based,
`page_size` default 50 (cap 200); responses `{items, page, page_size,
total}` where `total` is the count **after** filters. Orderings: facts
`created_at DESC, id DESC`; episodes `t_ref DESC`; events `ts DESC`; entities
fact-count DESC then name. Exception: `GET /views/namespaces` is
intentionally unpaginated (bounded by files on disk), ordered fact-count DESC
then name, and returns a bare array.

Endpoints (namespace-scoped where applicable):

- `GET /views/namespaces` — discovered from `*.db` (skipping `_*.db`), with
  per-namespace counts: facts latest/retired, entities, episodes,
  supersession chains, file size, top predicates, adds/searches in the last
  7 days (from `_events.db`; `origin:"ui"` excluded), earliest stored event ts.
- `GET /views/{ns}/facts?latest_only&predicate&entity&min_salience&q&page` —
  `entity` matches entity **name** (case-insensitive) joined via
  `subject_id`; `predicate` exact; `min_salience` float on the engine's 0–10
  scale (`salience >= value`); `q` uses the FTS5 index (text filter, distinct
  from real search). Rows carry `subject` = `entity.name`; objects display
  `object_literal` (`object_id` is effectively always NULL in current data).
- `GET /views/{ns}/facts/{fact_id}` — full row + supersession chain (walk
  `superseded_by` both directions) + source episode.
- `GET /views/{ns}/episodes?page` · `GET /views/{ns}/episodes/{id}` (with
  extracted facts).
- `GET /views/{ns}/entities?page` — names + fact counts (a list, not a graph:
  current data has literal-only objects and NULL entity types).
- `GET /views/{ns}/events?kind&page` — activity/traces; `kind` ∈ `add|search`
  only (failed calls keep their kind; `payload.error` marks them, §5).
- `POST /views/{ns}/test-search {query, k}` — the manual query box; real
  search via the writable path above; records the event with `origin:"ui"`,
  labeled in the UI as a live search that updates access stats.

## 8. Web UI

React 18 + TypeScript, Vite, built with Bun; react-router, Tailwind CSS v4,
Recharts (used sparingly — sparklines on Overview, the timeline visual).
Served from `console/src/lean_memory_console/static/`. Aesthetic direction
set at implementation time via the frontend-design skill — requirement: a
purposeful verification instrument, not an admin template and not a
metrics-wall.

Pages (namespace switcher in the header):

1. **Overview** — namespace cards: counts, top predicates, 7-day
   adds/searches sparkline, supersession rate and facts-per-add as plain
   numbers (the "microscope" compromise — no dashboard page). Docker mode
   shows the connect snippet here; empty state IS the connect snippet.
2. **Memories** — filterable/sortable fact table (fact_text, subject,
   predicate, object_literal, salience, confidence, is_latest, access_count,
   valid_at). Fact drawer: full metadata, **supersession timeline** (chain
   oldest→newest with valid intervals — the wedge visual), provenance episode.
3. **Episodes** — transcript (episode.raw by t_ref) → facts extracted per
   turn (the granularity window).
4. **Activity & Traces** — polled feed (3–5 s) of add/search events; add rows
   expand to facts created/superseded; search rows expand to the per-hit
   score decomposition (final = 0.6·relevance + 0.2·recency + 0.2·importance;
   dense/sparse ranks; RRF) + the test-query box. Shows the "connect via
   observing MCP" hint when no sidecar exists.

## 9. Distribution: Claude Code plugin (primary) + PyPI + Docker

**Plugin** (verified against current plugin docs): on extraction the repo
doubles as its own marketplace (`.claude-plugin/marketplace.json` at the
marketplace root — the directory containing `.claude-plugin/` — with plugin
`source: "./plugin"`).

- `.mcp.json`: the **stdio entry only** —
  `{"command": "uvx", "args": ["lean-memory-console", "mcp"]}`. The Docker
  HTTP connection is deliberately NOT a second auto-enabled entry (it would
  hard-fail config parse when `LM_API_KEY` is unset and spawn a dead
  connection otherwise); Docker users run the one-line
  `claude mcp add --transport http …` snippet the console displays. If Tier 2
  revisits this, `user_config` (keychain-stored key) is the documented path.
- `commands/`: `/memory:ui` (launch/open the local viewer),
  `/memory:status` (resolved data root, namespaces, connect snippets),
  `/memory:server-up|down` — resolves the compose file via
  `lean-memory-console --print-compose-path` (the copy shipped inside the
  installed package); **the plugin does not bundle its own copy**, so
  `deploy/docker-compose.yml` in the repo is the single source of truth.
- **WP8a lands here later**: `PreCompact` (save memories before compaction)
  and `SessionStart` (recall on start) hooks ship as a plugin update,
  post-signal, per the roadmap.

Install flow: `/plugin marketplace add <owner>/lean-memory-console` →
`/plugin install lean-memory`. Also distributed as plain PyPI
(`uvx lean-memory-console`) and Docker (below) for non-Claude-Code users.

## 10. Deployment

**Data-root resolution (one rule, both commands):** `--root` >
`LM_DATA_ROOT` > `~/.lean_memory`. The console serves exactly one root and
never auto-merges roots. Trap closed explicitly: the core engine's *own*
default root is `./lm_data` (not `~/.lean_memory`), so `/memory:status` and
the onboarding screen print the resolved root and warn when `./lm_data`
exists but is not the served root — a human must not silently inspect an
empty `~/.lean_memory` while their agent wrote to `./lm_data`.

**Local:** `uvx lean-memory-console serve [--root …] [--port 8377]` — binds
127.0.0.1, prints/opens the tokened URL, Ctrl-C to stop.
`lean-memory-console mcp [--root …]` for the observing wrapper.
`lean-memory-console --print-compose-path` prints the packaged compose file
path (§9).

**Docker (single-tenant):** `deploy/Dockerfile`, multi-stage with named final
targets: bun build stage → `FROM python:3.13-slim AS slim` (installs
`console/`, copies assets; never installs `[models]`; stub embedder) →
`FROM slim AS full` (adds `lean-memory[models]`, CPU torch, real embedder +
reranker). `docker-compose.yml` sets `build.target: full` — **full is the
default**; slim exists for API/UI development and is never the documented
first-run path (stub vectors would recreate the FakeEmbedder
first-impression failure the quality gate exists to fix).

Env: `LM_DATA_ROOT` (default `/data` in Docker), `LM_API_KEY` (**required in
Docker mode** — refuse to boot without it; unused in local mode), `PORT`
(default 8377), `LM_CONSOLE_MODELS` (`auto|stub`, default `auto`: real models
when `lean-memory[models]` is importable, else stubs; `stub` short-circuits
before any torch import). sqlite-vec is a hard dependency of both images (the
engine loads it at store-open) and is always boot-checked. Volumes: `/data`,
`~/.cache/huggingface` (full image).

Boot validation (both modes): data root writable (serve: readable),
`LM_API_KEY` set (Docker mode), sqlite-vec loadable; fail fast with a clear
message.

## 11. Error handling

- Empty data root → onboarding screen with connect snippets (plugin install,
  observing-MCP line, Docker snippet) and the resolved-root warning (§10),
  not empty tables.
- Stub embeddings (`LM_CONSOLE_MODELS` resolved to stub) → banner on
  Traces/Overview: "semantic scores are stub-generated".
- Missing sidecar (`_events.db` absent) → Traces page hint, not an error.
- Namespace file disappears between requests (user deleted it) → 404 with a
  friendly message; discovery refreshes on every `/views/namespaces` call.
- Malformed/oversized payloads → 422 structured error; engine exceptions →
  500 with the event still recorded (`payload.error`) where the event write
  itself succeeds — event-write failure degrades to a log line (§5), never a
  masked response.
- Concurrency: one `Memory` instance + one asyncio write lock per namespace
  **per process** (intra-process serialization only); the lock is held across
  add + supersession detection (§5). Cross-process safety is the §6 contract:
  console-owned `busy_timeout` + bounded retry-on-`SQLITE_BUSY` — the engine
  itself provides WAL only, no busy timeout.

## 12. Testing

`console/tests/` (own pytest config; core repo suite untouched and green):

- **Observing MCP:** in-process stdio round-trip — add → search returns the
  fact; event rows written to `_events.db`; `t_ref` supplied → `valid_at`
  matches; namespace `_events`/`_server`/empty-sanitizing rejected.
- **Supersession events:** two contradicting adds on one slot → second add's
  event carries the first fact's id in `superseded_fact_ids` (exact-id
  assertion).
- **Parity (wrapper vs core MCP):** the wrapper exposes exactly
  `{memory_add, memory_search}` (`memory_clear` intentionally absent); each
  shared tool accepts at least the core args (`namespace`, `text`/`query`,
  `k`); the wrapper's extras (`source`, `t_ref`, structured returns) are
  asserted as deliberate additions. Fails if the wrapper drops a core arg or
  grows a tool core lacks.
- **Auth:** Docker mode — no key → 401, wrong key → 401, `LM_API_KEY` missing
  at boot → exit non-zero. Local mode — request without `?token` → 401.
  `GET /views/whoami` body shape asserted in both modes.
- **Read path:** stats/facts/chain/episodes/entities endpoints against the
  committed fixture; pagination envelope shape (`total` = post-filter);
  `latest_only` default; open strategy — `mode=ro` succeeds with and without
  a `-wal` sidecar, and `immutable=1` is attempted only after an actual
  error-14 open failure.
- **Concurrency:** test-search returns hits while another connection holds a
  write on the namespace (stat-bump best-effort); two processes interleaving
  event INSERTs lose nothing (busy_timeout); retention boundary — 10 001st
  event prunes to exactly 10 000 and `/views/namespaces` surfaces the
  earliest survivor ts.
- **REST mirror parity** with the MCP tools; test-search records
  `origin:"ui"` and is excluded from Overview's 7-day aggregates.
- All offline, stub backends, no network, no model downloads.

**Fixture:** `console/tests/fixtures/build_fixture.py` (stub backends,
deterministic), output checked in; contents are the acceptance criteria:
2 namespaces, 2 episodes each, ≥1 supersession chain of length ≥2 (one
retired + one latest), ≥1 entity with 2 facts, 1 add event with
`superseded_count > 0`, 1 search event with a full score payload, 1 event
with `payload.error`. Rebuilt + re-committed when the mirrored schema changes.

Frontend gate: `bun run typecheck && bun run build`.

End-to-end verification (manual, pre-merge): install the plugin from the
local marketplace path, let a real Claude Code session store + search
memories through the observing MCP, open `/memory:ui`, verify every page
renders the resulting state; then `docker compose up`, connect via the HTTP
snippet, repeat (superpowers `verify` flow).

## 13. Migration path / engine-coupling debt

- **WP4 lands** → `inspect_sql.py` internals swap to
  `Memory.get/get_all/history` + `search(explain=True)`; supersession data
  comes from the API instead of the §5 stopgap. The `/views` shapes survive
  unchanged.
- **Engine `busy_timeout` (lane-A debt):** the proper long-term fix for the
  §6 cross-process contract is `PRAGMA busy_timeout` in the engine's
  `_connect()` — deferred behind the lane-D rule; the console's retry wrapper
  is the stopgap and can be deleted when the engine gains it.
- **Schema-mirror tripwire (fail-loud, not a comment):** the fingerprint is
  computed at test time from the **installed** `lean_memory` package's
  `store/schema.py` via `importlib.resources` (concatenated `CREATE`
  statements extracted from the file text, hashed) — never from a copy
  checked into `console/`. The expected digest is a checked-in constant;
  dependency drift turns the suite red. The same mechanism guards the
  `_SAFE_NS` mirror in `config.py` (fingerprint of `memory.py`'s sanitizer
  lines).
- **Tier 2 trigger** (§14) is demand, not time: multiple humans/teams asking
  for isolation and key management.

## 14. Tier 2 (deferred, design preserved from v1)

On demonstrated team demand, the Docker mode grows the v1 platform layer —
all of it already designed and reviewed in v1 of this spec (git history of
this file, commit `bb2948d`): tenant registry + hashed per-tenant API keys in
a `_server.db` control plane, tenant CRUD with the pinned delete lifecycle
(lock → registry cascade → close pooled instance → unlink db/wal/shm),
plane-scoped credentials (tenant keys vs admin token, wrong class → 401),
per-tenant event scoping, and the tenant management UI page. Nothing in Tier
1's architecture blocks it: namespaces become per-tenant namespace *sets*,
`_events.db` gains a tenant column, and the auth dependency swaps from the
single `LM_API_KEY` to key-resolution.

## 15. Risks

- **MCP streamable-HTTP + auth ergonomics in Claude Code** (Docker mode) —
  verify the `--header` bearer flow against a real session early; fall back
  to key-in-URL-path if header propagation proves unreliable. Local mode is
  unaffected (stdio).
- **Observing-wrapper drift vs core MCP server** — covered by the §12 parity
  test (name equality, core-args superset, deliberate-extras assertion); the
  wrapper tracks the `Memory` API, not the core tool signatures.
- **Cross-process writers** — the §6 retry contract is a stopgap, not a lock
  manager; pathological contention (many sessions hammering one namespace)
  degrades to retries and eventual `SQLITE_BUSY` surfacing in `payload.error`.
  Documented guidance: one namespace per project/session.
- **Image size (full)** — torch CPU wheels are heavy; documented; slim exists
  but is never the first-run path.
- **Event log growth** — 10k/namespace cap, atomic oldest-first pruning,
  truncation surfaced via earliest-ts (§5).
- **Engine evolves under us** — the schema-fingerprint tripwire (§13) turns
  silent drift into a red suite instead of a wrong UI.
