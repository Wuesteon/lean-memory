# lean-memory-server — agent-first memory service + verification UI

Date: 2026-07-10 · Status: approved design (brainstorming complete; adversarial
self-review applied)
Packet: **Memory UI** (workpackets.md status table) · Branch: `worktree-memory-ui` · Lane: **D**

## 1. Goal

A self-hostable Docker service that turns lean-memory into an agent-facing
memory backend with a human verification console:

- **Agents write and search.** Claude Code (or any MCP/HTTP client) connects
  over the network with a per-tenant API key. The agent is the only writer.
- **Humans observe and verify.** The web UI is read-only against memory data:
  metrics, live activity, fact browsing, supersession timelines, provenance
  episodes, and search traces with full score breakdowns. The only mutating
  actions in the UI are tenant/API-key management.

One tenant = one engine SQLite file (the engine's existing
one-file-per-namespace model), so tenant isolation is physical, not row-level.

## 2. Non-goals (v1)

- No user accounts / RBAC / audit log / webhooks / usage metering (bolt on later).
- No memory editing or deleting from the UI (ADD-only discipline; deletion is
  WP5's design problem, not this packet's).
- No changes to the core library, its MCP stdio server, its pyproject, its
  tests, or `bench/` (lane-D conflict rule — see §3).
- No SSE/websockets; the activity feed polls (3–5 s interval).
- No benchmark/project-status views (that audience is this repo's developers,
  not self-hosters).
- No component-level frontend tests in v1 (typecheck + production build are the
  CI gate).

## 3. Constraints and strategy context

- **Lane D file discipline.** This packet must not touch lane A
  (`src/lean_memory/`), lane B (`bench/`), or lane C files. Everything ships in
  new top-level directories. Consequence: the server does **not** add
  enumeration methods to the store; it runs its own read-only SQL, and
  migrates to WP4's `get/get_all/history/explain` API when that packet lands
  (see §12).
- **Anti-goals reconciliation.** `workpackets.md` lists "dashboards, hosted
  anything" as anti-goals *for the core library's embedded positioning*. This
  deliverable is a separate, optional deployable: the core library gains no
  mandatory server/daemon, no new dependencies, no changed defaults. That
  satisfies WP8's rule ("none may add a mandatory server/daemon to the core
  library"). Recorded here as a conscious, user-directed strategy addition.
- **Relationship to WP8b/WP8c.** The REST data plane here effectively delivers
  WP8b (thin FastAPI wrapper) early, in lane D. Its surface (including the §7
  pagination envelope) is shaped so WP8c (TypeScript client) can target it
  unchanged. At merge time, update the WP8b row in `workpackets.md` to point
  here.
- **Global invariants inherited:** offline test suite green at every commit;
  ADD-only discipline; offline-by-default (server runs fully with stub
  backends); Apache-2.0.

## 4. Repo layout (all new, no existing files modified)

```
server/                       # deployable FastAPI app (own project)
  pyproject.toml              # depends on lean-memory (path/PyPI), fastapi,
                              # uvicorn, mcp; extras: [models] passthrough
  src/lean_memory_server/
    app.py                    # FastAPI factory, static mount, lifespan
    config.py                 # env parsing (LM_DATA_ROOT, ADMIN_TOKEN, PORT,
                              # LM_SERVER_MODELS)
    control.py                # control-plane DB (tenants, keys, events)
    engine.py                 # Memory instance pool (one per tenant),
                              # per-tenant asyncio write locks
    events.py                 # event recording + supersession detection
    inspect_sql.py            # read-only enumeration SQL over tenant DBs
    routes/
      mcp.py                  # streamable-HTTP MCP endpoint (+ key auth)
      data.py                 # /v1 REST mirror (add/search)
      admin.py                # tenants, keys, whoami
      views.py                # read-only inspection API for the UI
    static/                   # built SPA output (gitignored; populated by the
                              # ui build in Docker/CI, or `bun run build` locally)
  tests/                      # offline pytest suite (own conftest)
    fixtures/build_fixture.py # deterministic fixture-DB builder (see §11)
  README.md                   # quickstart, Claude Code connect snippet
ui/                           # React SPA source
  package.json                # built with Bun; Vite, React 18, TS,
                              # react-router, Tailwind CSS v4, Recharts
  src/...
deploy/
  Dockerfile                  # multi-stage; named targets `slim` and `full` (§9)
  docker-compose.yml          # service + /data volume; build.target: full
```

## 5. Control plane storage

`_server.db` (SQLite, WAL) lives beside the tenant DBs in `LM_DATA_ROOT`
(default `/data` in Docker).

**Reserved-namespace guard (server-owned).** The engine's sanitizer does NOT
prevent collisions here: it is the private regex `_SAFE_NS =
re.compile(r"[^A-Za-z0-9_.-]")` plus an `… or "default"` empty-name fallback
(`memory.py:38,70-71`), and it *preserves* leading underscores — a tenant named
`_server` would map to `_server.db`. Therefore tenant creation (§7) must
reject any name whose sanitized namespace is empty or begins with `_`. The
server mirrors the regex + fallback in one place (`control.py`) with a header
comment naming `memory.py` as the source; this private-symbol coupling is
owned by the server and re-verified on every engine version bump.

```sql
tenant(id INTEGER PK, name TEXT UNIQUE, namespace TEXT UNIQUE,  -- sanitized
       created_at INTEGER,                                       -- epoch-ms
       deleted_at INTEGER NULL)                                  -- soft-mark, §7
api_key(id INTEGER PK, tenant_id INTEGER REFERENCES tenant ON DELETE CASCADE,
        key_hash TEXT,          -- sha256 hex of full key
        prefix TEXT,            -- first 12 chars, for display ("lm_live_ab12…")
        created_at INTEGER, revoked_at INTEGER NULL)
event(id INTEGER PK, tenant_id INTEGER REFERENCES tenant ON DELETE CASCADE,
      ts INTEGER, kind TEXT CHECK(kind IN ('add','search')),
      duration_ms REAL, payload TEXT)                            -- JSON
```

- API keys: `lm_live_` + 32 hex chars, shown **once** at creation, stored as
  sha256. Lookup: hash the presented key, match `key_hash`, reject if revoked.
- **Failed calls** keep their `add`/`search` kind — there is no `failed` kind.
  A failure is marked solely by `payload.error` (string); the Activity feed
  renders an error badge from it. The `?kind` filter (§7) accepts exactly
  `add|search`.
- **Control-plane concurrency:** all control-plane mutations for a tenant
  (create/delete/key ops) are serialized under that tenant's lock (§10). On
  tenant delete, the tenant row and its `api_key`/`event` rows are removed in
  one `_server.db` transaction *before* the data files are unlinked.

Event payloads:

- `add`: `{episode_text_chars, source, t_ref, fact_ids, fact_count,
  superseded_fact_ids, superseded_count}`.
  **Supersession detection (pinned):** immediately after `Memory.add()`
  returns — while still holding the per-tenant write lock, so no concurrent
  add can interleave — run
  `SELECT id FROM fact WHERE superseded_by IN (<returned fact ids>)`
  on the tenant DB. `superseded_fact_ids` = that result;
  `superseded_count = len(result)`. (`superseded_by` points from the retired
  fact to the new one, per `store.supersede_fact`.) Stopgap until WP4 exposes
  supersession in the return value.
- `search`: `{query, k, latest_only, origin, hits: [{fact_id, fact_text,
  final_score, relevance, recency, importance, dense_rank, sparse_rank,
  rrf_score}]}` — hit fields copied verbatim from `RetrievedFact`.
  `origin` is `"agent"` (data plane) or `"ui"` (test-search, §7).

**Naming note:** the wire parameter is `latest_only` everywhere (REST body,
views query param, event payload). It maps to the engine kwarg
`is_latest_only` and filters the `fact.is_latest` column. Default: `true`.

## 6. Data plane (agent-facing)

Auth: `Authorization: Bearer <api-key>` on every call; the key resolves the
tenant. **Credentials are plane-scoped:** a tenant key is valid only on
`/mcp` and `/v1`; the admin token is valid only on `/admin` and `/views`.
Presenting the wrong credential class returns 401 (never a super-key).

**MCP (primary).** `/mcp` — streamable-HTTP MCP endpoint (Python `mcp` SDK /
FastMCP mounted into the FastAPI app), tools mirroring the stdio server's
vocabulary (`memory_add`/`memory_search`; deliberately no `memory_clear` —
tenant deletion in the UI is the only deletion surface) but tenant-scoped and
JSON-returning:

- `memory_add(text: str, source: str = "user", t_ref: int | None = None)
  -> {fact_ids, superseded_count}`
- `memory_search(query: str, k: int = 5) -> {hits: [{fact_text, final_score}]}`
  — MCP always passes `latest_only=true`; the flag is REST-only (keeps the
  agent-facing surface minimal).

`t_ref` (epoch-ms) is the world/event time that becomes `valid_at` and anchors
the temporal spine. Live agents omit it (server fills `now`); replay/import
agents supply it — omitting it on historical data would silently collapse the
spine's ordering, so the README documents both modes.

Claude Code connect snippet (rendered ready-to-copy on the tenant page):

```bash
claude mcp add --transport http lean-memory http://<host>:8377/mcp \
  --header "Authorization: Bearer lm_live_…"
```

**REST mirror (secondary).** Same handlers, for non-MCP agents:

- `POST /v1/memories  {text, source?, t_ref?}` → `{fact_ids, superseded_count}`
- `POST /v1/search    {query, k?, latest_only?}` → full hit objects with
  score breakdown (richer than the MCP tool on purpose; MCP output stays
  small for agent context windows).

Both paths: acquire the tenant write lock (adds only), call the engine, time
it, record the event (§5), return. `Memory.search`'s access-stat bump
(`touch()`) is **correct** service behavior here (live usage signal), not a
defect.

## 7. Control + observation plane (human-facing)

Auth: `Authorization: Bearer <ADMIN_TOKEN>` on **all** `/admin` and `/views`
routes, including test-search and metrics.

**Login flow:** `GET /admin/whoami` returns 200 if the bearer matches
`ADMIN_TOKEN`, else 401. The SPA login screen validates the typed token
against it, then keeps the token in a React context (module memory) — never
localStorage, never a cookie, never a URL. Consequence (accepted for v1): a
full page reload requires re-entering the token.

Management:

- `POST /admin/tenants {name}` — validation: reject empty/whitespace-only
  names (422); compute the sanitized namespace; reject with 409 if the
  namespace is empty, begins with `_`, or already exists (even under a
  different display name — the namespace is the physical file, so namespace
  collision is the real constraint); 409 on duplicate display name.
- `GET /admin/tenants` · `POST /admin/tenants/{id}/keys` → full key, once ·
  `DELETE /admin/keys/{key_id}` (revoke)
- `DELETE /admin/tenants/{id}` — **pinned lifecycle:** (1) acquire the
  tenant's write lock; (2) delete tenant + keys + events in one `_server.db`
  transaction (new data-plane calls now 401/404); (3) evict the pooled
  `Memory` instance and `close()` it; (4) unlink `<ns>.db`, `<ns>.db-wal`,
  `<ns>.db-shm`; (5) release the lock. In-flight calls holding the lock
  complete first; a subsequent add must 404, not recreate the file. UI guards
  with a name-retype confirm dialog. (This is namespace purge, which WP5's
  design brief already recognizes as "trivially true" and the tenant-level
  deletion answer; the ADD-only invariant governs per-fact history *within* a
  store.)

**Pagination envelope (shared by every list endpoint, binding for WP8c):**
`page` is 1-based, `page_size` defaults to 50 (cap 200); responses are
`{items, page, page_size, total}`. Deterministic orderings: facts by
`created_at DESC, id DESC`; episodes by `t_ref DESC`; events by `ts DESC`;
entities by fact count DESC, then name.

Read-only inspection (all against `mode=ro` SQLite connections — safe because
this server process is the sole owner of `/data`, and WAL allows concurrent
readers):

- `GET /views/tenants/{id}/stats` — fact counts (latest/retired), entities,
  episodes, supersession chains, DB file size, top predicates
- `GET /views/tenants/{id}/facts?latest_only&predicate&entity&min_salience&q&page`
  — filter semantics: `entity` matches entity **name** (case-insensitive),
  resolved to `subject_id` via a join; `predicate` is an exact match;
  `min_salience` is a float on the engine's 0–10 salience scale filtering
  `salience >= value`; `latest_only` defaults `true`; `q` uses the FTS5 index
  (text filtering, distinct from real search). Response rows carry
  `subject` = `entity.name` joined on `subject_id`, and objects display
  `object_literal` (`object_id` is effectively always NULL in current data).
- `GET /views/tenants/{id}/facts/{fact_id}` — full row + supersession chain
  (walk `superseded_by` both directions) + source episode
- `GET /views/tenants/{id}/episodes?page` and `/episodes/{id}` (with extracted
  facts)
- `GET /views/tenants/{id}/entities?page` — names + fact counts (no graph;
  current data has literal-only objects and NULL entity types)
- `GET /views/tenants/{id}/events?kind&page` — activity feed / traces
  (`kind` ∈ `add|search`)
- `GET /views/tenants/{id}/metrics?window=` — `window` is a validated enum
  `1d|7d|30d` (default `7d`): adds/searches per day, facts-per-add,
  supersession rate, search latency p50/p95, aggregated from `event`.
  Events with `origin:"ui"` are **excluded** from aggregates (operator poking
  is not agent usage) but appear in the feed. The response includes the
  earliest event `ts` still stored, so the UI labels the true window when the
  §13 retention cap has truncated history.
- `POST /views/tenants/{id}/test-search {query, k}` — the UI's manual query
  box; runs a real search via the engine, records the event with
  `origin:"ui"`, labeled in the UI as a live search that updates access stats

## 8. Web UI

React 18 + TypeScript, Vite, built with Bun; react-router, Tailwind CSS v4,
Recharts. Served by FastAPI from `server/src/lean_memory_server/static/`.
Aesthetic direction set at implementation time via the frontend-design skill —
requirement: it must read as a purposeful observability console, not a
default admin template.

Pages (tenant switcher in the header, admin login screen up front):

1. **Dashboard** — per-tenant stat tiles (facts latest/retired, entities,
   episodes, chains) + charts: activity over time, facts-per-add, search
   latency, supersession rate.
2. **Activity** — polled feed (3–5 s) of add/search events, newest first,
   error badge on `payload.error`; add events expand to the facts
   created/superseded, search events link to the trace view.
3. **Memories** — filterable/sortable fact table (fact_text, subject,
   predicate, object_literal, salience, confidence, is_latest, access_count,
   valid_at). Fact detail drawer: full metadata, **supersession timeline**
   (chain rendered oldest→newest with valid intervals), provenance episode.
4. **Episodes** — transcript list (episode.raw, ordered by t_ref) → per-episode
   extracted facts (exposes the facts-per-turn granularity).
5. **Traces** — search events with expandable per-hit score decomposition
   (final = 0.6·relevance + 0.2·recency + 0.2·importance; dense/sparse ranks;
   RRF) + the manual test-query box.
6. **Tenants** — list, create, delete (guarded), API keys (issue/revoke), the
   Claude Code / REST connect snippets.

## 9. Deployment

`deploy/Dockerfile`, multi-stage with **named final targets**:

1. `oven/bun` stage: `bun install && bun run build` in `ui/` → static assets.
2. `FROM python:3.13-slim AS slim` — installs `server/` (which installs
   `lean-memory` from the repo checkout at build time) + copies assets.
   Never installs `[models]`; stub embedder; tiny image; vector scores are
   semantically meaningless and the UI labels them (§10).
3. `FROM slim AS full` — adds `lean-memory[models]` (CPU torch, real Qwen3
   embedder + reranker).

Build: `docker build --target slim|full`. `docker-compose.yml` sets
`build.target: full`; README documents both and the full image's size +
first-run model download into a cached volume.

Env: `LM_DATA_ROOT` (default `/data`), `ADMIN_TOKEN` (**required** — server
refuses to boot without it), `PORT` (default 8377), `LM_SERVER_MODELS`
(`auto|stub`, default `auto`): `auto` uses real models when
`lean-memory[models]` is importable (full image), else stubs; `stub` forces
the stub embedder and short-circuits **before any torch import**, so the full
image can run the offline test path. sqlite-vec is a hard dependency of both
images (the engine loads it at store-open; the vec0 index is always real —
only embedding *quality* differs) and is always boot-checked.

Volumes: `/data` (tenant DBs + `_server.db`), `~/.cache/huggingface` (full
image model cache).

## 10. Error handling

- No tenants yet / empty tenant → onboarding screen with the connect snippet,
  not empty tables.
- Stub-embedded data → banner on Traces/Dashboard: "semantic scores are
  stub-generated" (detected from the resolved `LM_SERVER_MODELS` mode).
- Known engine issue surfaced honestly: recency component reads ≈0 on
  historical timestamps (dead-recency bug on the WP0 backlog) — trace view
  footnotes it rather than hiding it.
- Concurrency: `engine.py` keeps one `Memory` instance per tenant and an
  asyncio lock per tenant. Writes (`add`, delete lifecycle, key mutations)
  take the lock; reads don't. The lock is held across add + supersession
  detection (§5).
- Malformed/oversized payloads → 422 with structured error; engine exceptions
  → 500, with the event still recorded (`payload.error`) so failures are
  visible in the activity feed.
- Boot-time validation: `LM_DATA_ROOT` writable, `ADMIN_TOKEN` set, sqlite-vec
  loadable; fail fast with a clear message.

## 11. Testing

`server/tests/` (own pytest config; core repo suite untouched and green):

- Auth: no key → 401; revoked key → 401; key of tenant A on tenant B's data →
  401/404 (isolation is the highest-value test). **Plane-scoping:** admin
  token on `POST /v1/memories` → 401; tenant key on `GET /views/...` → 401.
- Tenant lifecycle: duplicate name → 409; `"a b"` vs `"a/b"`
  (namespace-collision after sanitization) → 409; `_server` and
  empty-sanitizing names rejected; delete removes `.db`/`.db-wal`/`.db-shm`,
  cascades keys/events, and a subsequent add 404s without recreating the file.
- Round-trip: MCP add → search returns the fact; REST mirror parity; `t_ref`
  supplied → `valid_at` matches it.
- Events: two contradicting adds on one slot → second add's event has the
  first fact's id in `superseded_fact_ids` (exact-id assertion); search event
  records the full score payload with `origin:"agent"`; test-search records
  `origin:"ui"` and is excluded from metrics aggregates.
- Inspection SQL: stats/facts/chain/episodes endpoints against the committed
  fixture DB.
- Boot validation: missing ADMIN_TOKEN → exit non-zero.
- All offline, stub backends only, no network, no model downloads.

**Fixture DB:** built once by `server/tests/fixtures/build_fixture.py` (stub
backends, deterministic) and checked in. Minimum contents — these are the
inspection tests' concrete assertions: 1 tenant, 2 episodes, ≥1 supersession
chain of length ≥2 (one retired + one latest, so chain-walk is exercised in
both directions), ≥1 entity with 2 facts, 1 recorded add event with
`superseded_count > 0`, 1 search event with a full score payload. The builder
is re-run and the `.db` re-committed whenever the schema mirrored by
`inspect_sql.py` changes.

Frontend gate: `bun run typecheck && bun run build` in CI; no component tests
in v1.

End-to-end verification (manual, pre-merge): `docker compose up`, create a
tenant, connect a real Claude Code session via MCP, store + search memories,
verify every UI page renders the resulting state (superpowers `verify` flow).

## 12. Migration path / future

- **WP4 lands** → replace `inspect_sql.py` internals with
  `Memory.get/get_all/history` and `search(explain=True)`, and take
  supersession data from the API instead of the §5 stopgap query; the
  `/views` API shape is designed to survive that swap unchanged.
- **WP8c** → the TS client targets `/v1` and the §7 pagination envelope as-is.
- Later candidates (explicitly out of v1): SSE live feed, per-tenant usage
  quotas, RBAC, redaction tooling once WP5's deletion semantics are approved.

## 13. Risks

- **MCP streamable-HTTP + auth ergonomics in Claude Code** — verify the
  `--header` bearer flow against a real session early (plan task 1 risk
  spike), fall back to key-in-URL-path (`/mcp/<key>`) if header propagation
  proves unreliable.
- **Image size (full)** — torch CPU wheels are heavy; acceptable and
  documented, `slim` exists for everything but semantic search.
- **Event log growth** — search payloads store k hits each. Retention: hard
  cap of 10k events per tenant, pruned oldest-first on write. Metrics are
  computed from whatever events remain; the metrics response's earliest-`ts`
  field (§7) makes the truncation visible instead of silent.
- **Engine evolves under us** (lane A moves fast) — the server pins
  `lean-memory` to the repo checkout at Docker build time; the read-only SQL
  and the `_SAFE_NS` mirror are duplicated by necessity until WP4, and
  `inspect_sql.py`/`control.py` carry header comments naming the engine files
  they mirror, re-verified on every engine bump.
