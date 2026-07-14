# lean-memory-console

A read-only verification console for [lean-memory](../README.md).

**Agents write and search; the human verifies.** Your agent (Claude Code via
the observing MCP, or any HTTP client in Docker mode) is the only writer of
memory content. You open the console **read-only over stored memory** — no
adding, editing, or deleting facts — to see the engine's invisible signals for
the first time: the ADD-only supersession spine, per-hit score decomposition,
and provenance episodes.

> The single write exception is the manual **test-search box**: it runs a real
> engine search and therefore bumps access stats (`touch()`). That is
> observability of live-search behavior, not a memory mutation.

## Two modes

### Local (default — zero Docker, nothing runs when the agent doesn't)

Two transient localhost processes over one shared data root:

```bash
# 1. The observing MCP wrapper — your agent stores + searches through this.
uvx lean-memory-console mcp

# 2. The console — you open this to verify. Binds 127.0.0.1, prints a
#    tokened URL, Ctrl-C to stop.
uvx lean-memory-console serve
```

`serve` accepts `--root`, `--port`, and `--no-open` (skip the browser launch).
On load the SPA captures the token from `?token=…` — held in memory and sent as
the `X-Console-Token` header on every request — and strips it from the address
bar (`history.replaceState`). There is no cookie; a page refresh without the
token drops the session.

Via the Claude Code plugin (recommended), the MCP is wired automatically:

```
/plugin marketplace add Wuesteon/lean-memory-console
/plugin install lean-memory
/memory:ui          # launch + open the console
/memory:status      # resolved root, namespaces, connect snippets
/memory:server-up   # start the Docker data plane
/memory:server-down # stop it (the data volume persists)
```

(From a local checkout you can register the marketplace with
`/plugin marketplace add ./` at the repo root instead of the GitHub slug.)

### Docker (single-tenant, long-running, container owns /data)

```bash
export LM_API_KEY=$(openssl rand -hex 24)     # REQUIRED — the container refuses to boot without it
docker compose -f "$(lean-memory-console --print-compose-path)" up -d
```

Then connect an agent over streamable-HTTP MCP and open the UI:

```bash
claude mcp add --transport http lean-memory http://127.0.0.1:8377/mcp \
  --header "Authorization: Bearer $LM_API_KEY"
# UI: http://127.0.0.1:8377/  (authenticate with the same LM_API_KEY)
```

Docker mode binds `0.0.0.0` so the published port is reachable; there is **no**
local-mode Host guard and no per-launch session token. The controls are the
bearer gate (`LM_API_KEY`) plus the MCP transport-security Host allowlist. If
you reach the container over a LAN hostname/IP (no reverse proxy), list the
host(s) in `LM_MCP_ALLOWED_HOSTS` (comma-separated; `:*` matches any port) or
the `/mcp` mount 421s on its Host check — a commented example sits in the
packaged `docker-compose.yml`. The compose service builds the
`full` image target; container env is `LM_DATA_ROOT` / `LM_API_KEY` (required) /
`PORT` / `LM_CONSOLE_MODELS` / `LM_MCP_ALLOWED_HOSTS`.

A plain REST mirror exists for non-MCP agents:
`POST /v1/{namespace}/memories` and `POST /v1/{namespace}/search`.

## The data-root rule (read this — it is the #1 trap)

The console serves **exactly one** data root and never auto-merges roots.
Resolution order, both `serve` and `mcp`:

```
--root  >  $LM_DATA_ROOT  >  ~/.lean_memory
```

**The ./lm_data trap.** The core `lean-memory` engine's *own* default root is
`./lm_data` (a directory in your current working directory), **not**
`~/.lean_memory`. So if you ran the engine directly (or the core stdio MCP
server pointed at the default) your memories may be under `./lm_data` while the
console defaults to `~/.lean_memory` — and you would inspect an empty root.

- `/memory:status` **prints the resolved root** and **warns** when `./lm_data`
  exists in the working directory but is not the served root.
- Fix: point the console at the right root, e.g.
  `uvx lean-memory-console serve --root ./lm_data`, or set
  `export LM_DATA_ROOT=$(pwd)/lm_data`.

The console adds exactly one file to the data root: `_events.db` (search/add
traces). Everything else is the engine's own `<namespace>.db` files.

## Live vs. replay: the `t_ref` rule

`t_ref` (epoch-ms) is the **world/event time** that becomes a fact's `valid_at`
and anchors the temporal supersession spine.

- **Live agents omit it.** The observing MCP fills `now`, so facts are ordered
  by wall-clock ingest — correct for an agent capturing memories as they happen.
- **Replay / import supplies it.** When backfilling historical conversations,
  pass the original event time as `t_ref` (epoch-ms) on every add. **Omitting
  `t_ref` on historical data silently collapses the spine's ordering** — every
  backfilled fact gets `now`, so supersession and point-in-time queries become
  meaningless.

Rule of thumb: if you are importing anything that did not happen right now,
set `t_ref`.

## One namespace per project

Namespaces replace tenants: the engine stores one SQLite file per namespace,
which is the isolation boundary. Use **one namespace per project/session**.

Cross-process writers on a *single* namespace (e.g. two Claude Code sessions
spawning the wrapper on the same data root) are supported but serialized by a
bounded retry-on-`SQLITE_BUSY` loop — not a lock manager. Pathological
contention degrades to retries and, eventually, a `SQLITE_BUSY` surfaced in the
event's `payload.error`. Keeping namespaces per-project avoids the contention
entirely.

Namespaces are created implicitly on first accepted `memory_add`. There is no
create-namespace step, and no delete-namespace surface (ADD-only discipline —
to remove a namespace, delete its `.db` file while nothing is running).
Namespaces whose sanitized name starts with `_` are **rejected** (the
`_events.db` sidecar is reserved); an empty name is not rejected — it sanitizes
to `default`.

## Image size (Docker `full`)

The default Docker image is the `full` target: it installs `lean-memory[models]`
(CPU **torch** + sentence-transformers + a cross-encoder reranker), so the image
is large (~1.46 GB) and the first run downloads model weights into the mounted
HF cache volume. The CPU torch build is pinned from
`download.pytorch.org/whl/cpu` so the resolve never pulls the multi-GB CUDA
wheels. This is deliberate — stub vectors would recreate the FakeEmbedder
first-impression failure the quality gate exists to fix. A `slim` target
(~365 MB, stub embedder, no models) exists for API/UI development
(`docker build --target slim …`) but is never the documented first-run path.

## Offline by default

The console runs fully on deterministic stub backends with no network and no
model downloads (`LM_CONSOLE_MODELS=stub`, or `auto` when `[models]` is not
importable). When scores are stub-generated the UI shows a banner saying so.

## License

Apache-2.0

## Manual E2E verification (pre-merge)

Run this before merge. It is a **manual** gate, not automated — steps that need
a live interactive Claude Code session are marked `[manual]` and are driven by a
human, not by tooling. Capture the observed outcome next to each step.

**Local mode (plugin + observing MCP):**

- [ ] `[manual]` `/plugin marketplace add ./` (from the repo root marketplace),
      then `/plugin install lean-memory`.
      Expected: plugin installs; the `lean-memory` MCP server appears in the
      session's MCP list, and `/memory:ui|status|server-up|server-down` are
      offered as commands.
- [ ] `[manual]` In a real Claude Code session, ask the agent to store a couple
      of facts and then search them (through the observing MCP
      `memory_add`/`memory_search`).
      Expected: `memory_add` returns `{fact_ids, superseded_count}`;
      `memory_search` returns hits whose `fact_text` matches what was stored;
      rows land in `<root>/_events.db`.
- [ ] `[manual]` Run `/memory:ui`.
      Expected: browser opens `http://127.0.0.1:8377/?token=…`; the address bar
      loses `?token` after boot; no login screen (local mode).
- [ ] `[manual]` Verify all four pages render the resulting state:
      - **Overview** — the namespace card shows fact counts, top predicates,
        the 7-day adds/searches sparkline, supersession rate, and facts-per-add.
      - **Memories** — the fact table lists the stored facts; opening a fact
        drawer shows metadata, the supersession timeline (chain oldest→newest),
        and the provenance episode.
      - **Episodes** — the transcript shows the stored turns, each expanding to
        the facts extracted from it.
      - **Activity & Traces** — the polled feed shows the `add`/`search` events;
        a search row expands to the per-hit score decomposition
        (`0.6·relevance + 0.2·recency + 0.2·importance`, dense/sparse ranks,
        RRF); the test-query box runs a live search and its row is labeled
        `origin: ui`.

**Docker mode (HTTP data plane):**

- [ ] `[manual]` `export LM_API_KEY=$(openssl rand -hex 24)` then
      `docker compose -f "$(lean-memory-console --print-compose-path)" up -d`
      (or `/memory:server-up`).
      Expected: container starts; boot validation passes (data root writable,
      `LM_API_KEY` set, sqlite-vec loadable).
- [ ] `[manual]` `claude mcp add --transport http lean-memory
      http://127.0.0.1:8377/mcp --header "Authorization: Bearer $LM_API_KEY"`.
      Expected: the MCP connection registers; a store+search from the agent
      succeeds and records events in the container's `/data/_events.db`.
- [ ] `[manual]` Open `http://127.0.0.1:8377/`.
      Expected: the login screen prompts for the key; entering `LM_API_KEY`
      authenticates; all four pages render the same as local mode, and the
      Overview shows the Docker connect snippet.
- [ ] `[manual]` `/memory:server-down`.
      Expected: container stops; the `lm_data` volume persists (re-`up` shows
      the same memories).
