# lean-memory-console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `lean-memory-console` — an agent-first memory verification console: a transient local web viewer + observing stdio MCP wrapper (search-trace sidecar) + single-tenant Docker mode, distributed primarily as a Claude Code plugin.

**Architecture:** One FastAPI app serving two modes (local: 127.0.0.1 + session token; Docker: single `LM_API_KEY` bearer). Agents write through an observing MCP wrapper (local, stdio) or the HTTP data plane (Docker), both funneling through one `EngineGateway` that wraps `lean_memory.Memory`, detects supersessions, and records events to an `_events.db` sidecar. The human-facing `/views` API reads engine DBs via read-only SQL until WP4 lands. A React/TS SPA (built with Bun into the package's `static/`) renders Overview, Memories (supersession timeline), Episodes, and Activity & Traces.

**Tech Stack:** Python ≥3.10 (FastAPI, uvicorn, `mcp` SDK, stdlib sqlite3), lean-memory (editable path dep, stub backends in tests), React 18 + TypeScript + Vite + Bun + Tailwind CSS v4 + react-router + Recharts, Docker multi-stage (`slim`/`full` targets).

**Spec:** `docs/superpowers/specs/2026-07-10-memory-ui-design.md` (design v2). Where this plan and the spec disagree, the spec wins — flag the conflict instead of guessing.

## Global Constraints

- **Lane D:** never modify `src/lean_memory/`, `bench/`, `tests/` (core), or the root `pyproject.toml`. All work lives in `console/`, `ui/`, `plugin/`, `deploy/`, `.claude-plugin/`.
- **Rebase gate (spec §3):** this plan's execution starts only after the packet rebases onto post-WP0 main. Do not start Task 1 before that rebase.
- **Offline:** every test runs with stub backends — no network, no model downloads. Core suite must stay green: `.venv/bin/python -m pytest tests/ -q` (91 passing at plan time).
- **Console venv:** all backend commands use `console/.venv` created in Task 1 (`python3 -m venv console/.venv && console/.venv/bin/pip install -e . -e './console[dev]'` from repo root). Never install into the repo's `.venv`.
- **Naming:** wire parameter is `latest_only` everywhere (maps to engine kwarg `is_latest_only`, column `fact.is_latest`); default `true`. Pagination envelope `{items, page, page_size, total}`, `page` 1-based, `page_size` default 50 cap 200. Port default **8377**. Event kinds exactly `add|search`. Sidecar file `_events.db`; namespace discovery skips `_*.db`; namespaces whose sanitized form starts with `_` are rejected.
- **Cross-process safety is console-owned (spec §6):** engine sets no `busy_timeout` — console connections set `PRAGMA busy_timeout=5000`; every engine write goes through `retry_busy` (3 attempts). Read-only opens are `mode=ro` first, `immutable=1` only after an actual `OperationalError`.
- **License Apache-2.0; conventional commit messages; commit after every green task.**

## File Structure

```
console/                                  # Python package (own project)
  pyproject.toml                          # hatchling; console script lean-memory-console
  src/lean_memory_console/
    __init__.py                           # __version__
    config.py                             # sanitizer mirror, reserved-ns guard, data-root resolution, ConsoleConfig
    events.py                             # EventLog: _events.db sidecar (record/list/summary/atomic retention)
    engine.py                             # EngineGateway: Memory pool, per-ns locks, retry_busy, supersession detection
    inspect_sql.py                        # read-only enumeration SQL + schema/sanitizer fingerprint tripwires
    observe_mcp.py                        # observing stdio MCP wrapper (memory_add/memory_search superset)
    app.py                                # FastAPI factory, auth by mode, whoami, middleware, static mount
    routes/views.py                       # /views/* human-facing read API + test-search
    routes/data.py                        # /v1/* REST mirror (Docker data plane)
    routes/mcp.py                         # /mcp streamable-HTTP MCP (Docker data plane)
    cli.py                                # serve | mcp | --print-compose-path; boot validation
    static/                               # built SPA (gitignored; produced by ui/ build)
  tests/                                  # offline pytest suite + fixtures/build_fixture.py + committed fixture root
  README.md
ui/                                       # React 18 + TS SPA (Bun + Vite + Tailwind v4)
  src/api.ts                              # typed client, mode/auth handling
  src/App.tsx + src/pages/{Overview,Memories,Episodes,Activity}.tsx
plugin/                                   # Claude Code plugin
  .claude-plugin/plugin.json  .mcp.json  commands/
.claude-plugin/marketplace.json           # repo doubles as marketplace (source: "./plugin")
deploy/
  Dockerfile                              # bun build → slim → full (named targets)
  docker-compose.yml                      # single source of truth; packaged into the wheel
```

Task map: **1–4** backend foundation (scaffold/config → events → fixture + inspect part 1 → inspect part 2) · **5–9** backend services (gateway → observing MCP + parity → views app → data plane → CLI) · **10–14** frontend (scaffold/api client → Overview → Memories → Episodes → Activity & Traces) · **15–17** distribution (Docker → plugin → README + E2E gate).

---
# Backend Foundation (Tasks 1–4)

Lane D. All console code lives under `console/`. **No task in this section may touch
`src/lean_memory/`, `bench/`, `tests/`, or the root `pyproject.toml`.** Run every
command from the repo root
`/Users/wuesteon/research/lean-memory/.claude/worktrees/memory-ui` using
`console/.venv/bin/python`.

Ground truth baked from source (do not re-derive):

- `lean_memory.Memory(root=..., *, embedder=None, reranker=None, generator=None,
  router=None, typer=None, contradiction=None)` — every backend defaults to its
  OFFLINE stub (`FakeEmbedder`, `IdentityReranker`, `StubCandidateGenerator`,
  `RecallBiasedRouter`, `StubTyper`, `ContradictionResolver`). Passing **no**
  overrides already yields the fully-stub engine, so `models=="stub"` == default
  construction. `Memory.__init__` calls `self.root.mkdir(parents=True,
  exist_ok=True)` (memory.py:54). (memory.py:41–65)
- `Memory.add(namespace, text, *, t_ref=None, source="user") -> list[str]` returns
  the ids of facts written. (memory.py:78–134)
- `Memory.search(namespace, query, k=5, *, as_of=None, is_latest_only=True) ->
  list[RetrievedFact]`. (memory.py:173–186)
- `RetrievedFact` (types.py:95–107): `.fact` (a `Fact` with `.id`, `.fact_text`),
  `.final_score`, `.relevance`, `.recency`, `.importance`, `.dense_rank`,
  `.sparse_rank`, `.rrf_score`. All seven score fields are top-level attrs;
  `fact_id`/`fact_text` come from the nested `.fact`.
- Sanitizer (memory.py:38, 70–71): `_SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")`;
  `safe = _SAFE_NS.sub("_", namespace) or "default"`; store file is
  `root / f"{safe}.db"`.
- Engine `_connect()` (sqlite_store.py:50–60) sets only `PRAGMA journal_mode=WAL`
  and `PRAGMA foreign_keys=ON` — **no `busy_timeout`** (confirms spec §6; console
  owns all cross-process write-safety).
- `schema.py` table/column names (verbatim — SQL in `inspect_sql.py` must match):
  - `episode(id, namespace, raw, source, t_ref, created_at)`
  - `entity(id, namespace, name, type, summary, resolved_id, created_at)`
  - `fact(id, namespace, subject_id, predicate, object_id, object_literal,
    fact_text, valid_at, valid_to, superseded_by, is_latest, ingested_at,
    expired_at, invalidated_by, confidence, salience, last_access, access_count,
    is_inference, tier, episode_id, created_at)`
  - `fact_fts` (fts5): columns `fact_id UNINDEXED, fact_text`; query with
    `fact_fts MATCH ?`.
  - Supersession: `fact.superseded_by` points from the **retired** fact to the
    **new** fact; `fact.is_latest` is 1 for the head of a chain, 0 for retired.

---

### Task 1: console package scaffold + `config.py`

**Files:**
- Create: `console/pyproject.toml`
- Create: `console/src/lean_memory_console/__init__.py`
- Create: `console/src/lean_memory_console/config.py`
- Create: `deploy/docker-compose.yml` (single source of truth referenced by the
  wheel force-include; see DEVIATION 1)
- Create: `console/README.md`
- Test: `console/tests/__init__.py`, `console/tests/test_config.py`

**Interfaces:**
- Consumes: `lean_memory.memory._SAFE_NS` semantics (mirrored, not imported —
  the tripwire in Task 3 guards the mirror).
- Produces: `SAFE_NS_RE`, `sanitize_namespace`, `is_reserved_namespace`,
  `ns_db_path`, `resolve_data_root`, `ConsoleConfig`, `load_config`.

- [ ] **Step 1: Create the deploy compose file (force-include target).**
  The wheel force-include in Task 1's `pyproject.toml` copies
  `../deploy/docker-compose.yml` into the package; hatchling errors if the source
  path is missing, so it must exist before the first build. Write
  `deploy/docker-compose.yml` (repo-root `deploy/`, NOT under `console/`):
  ```yaml
  # deploy/docker-compose.yml — single-tenant lean-memory-console (Docker mode).
  #
  # Usage:
  #   LM_API_KEY=$(openssl rand -hex 24) docker compose -f deploy/docker-compose.yml up -d
  #
  # Single source of truth (spec §9): the console CLI packages this exact file
  # into the wheel; the plugin's /memory:server-up|down commands resolve it via
  #   docker compose -f "$(lean-memory-console --print-compose-path)" up -d
  services:
    console:
      build:
        context: ..
        dockerfile: deploy/Dockerfile
        target: full            # full is the default first-run image (real models)
      ports:
        - "8377:8377"
      environment:
        # LM_API_KEY is required in Docker mode — compose refuses to start
        # without it (the console also boot-checks it).
        - LM_API_KEY=${LM_API_KEY:?set LM_API_KEY - e.g. openssl rand -hex 24}
        - LM_DATA_ROOT=/data
        - PORT=8377
        - LM_CONSOLE_MODELS=auto
      volumes:
        - lm_data:/data
        - hf_cache:/root/.cache/huggingface
      restart: unless-stopped

  volumes:
    lm_data:
    hf_cache:
  ```

  This is the FINAL content (byte-identical to what Task 15 verifies with
  static tests) — the Dockerfile it references is created in Task 15.

- [ ] **Step 2: Write `console/pyproject.toml`.**
  ```toml
  [build-system]
  requires = ["hatchling"]
  build-backend = "hatchling.build"

  [project]
  name = "lean-memory-console"
  version = "0.0.1"
  description = "Agent-first read-only verification console for lean-memory"
  requires-python = ">=3.10"
  dependencies = [
      "lean-memory",
      "fastapi>=0.115",
      "uvicorn>=0.30",
      "mcp>=1.0",
  ]

  [project.optional-dependencies]
  dev = ["pytest>=8", "httpx", "pyyaml"]

  [project.scripts]
  lean-memory-console = "lean_memory_console.cli:main"

  [tool.hatch.build.targets.wheel]
  packages = ["src/lean_memory_console"]

  [tool.hatch.build.targets.wheel.force-include]
  "../deploy/docker-compose.yml" = "lean_memory_console/deploy/docker-compose.yml"

  [tool.pytest.ini_options]
  testpaths = ["tests"]
  ```

- [ ] **Step 3: Write the package `__init__.py`.**
  `console/src/lean_memory_console/__init__.py`:
  ```python
  """lean-memory-console — agent-first read-only verification console."""

  __version__ = "0.0.1"
  ```

- [ ] **Step 4: Create the console venv and install (editable core + console[dev]).**
  ```bash
  python3 -m venv console/.venv
  console/.venv/bin/pip install -e . -e './console[dev]'
  ```
  Expected: both `lean-memory` and `lean-memory-console` install editable; the
  `force-include` resolves because `deploy/docker-compose.yml` now exists (Step 1).

- [ ] **Step 5: Write the failing test `console/tests/test_config.py`.**
  Also create empty `console/tests/__init__.py`.
  ```python
  import re

  import pytest

  from lean_memory_console import config as cfg


  def test_safe_ns_re_mirrors_engine_charclass():
      # Mirror of memory.py:38  _SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")
      assert isinstance(cfg.SAFE_NS_RE, re.Pattern)
      assert cfg.SAFE_NS_RE.pattern == r"[^A-Za-z0-9_.-]"


  @pytest.mark.parametrize(
      "raw,expected",
      [
          ("a b", "a_b"),
          ("", "default"),
          ("_events", "_events"),  # leading underscore preserved (engine parity)
          ("Project.One-2", "Project.One-2"),
          ("weird/slash:x", "weird_slash_x"),
      ],
  )
  def test_sanitize_namespace_matches_engine(raw, expected):
      assert cfg.sanitize_namespace(raw) == expected


  @pytest.mark.parametrize(
      "raw,reserved",
      [
          ("_events", True),
          ("__x", True),
          ("a", False),
          ("", False),  # sanitizes to "default", not reserved
      ],
  )
  def test_is_reserved_namespace(raw, reserved):
      assert cfg.is_reserved_namespace(raw) is reserved


  def test_ns_db_path(tmp_path):
      assert cfg.ns_db_path(tmp_path, "a b") == tmp_path / "a_b.db"
      assert cfg.ns_db_path(tmp_path, "") == tmp_path / "default.db"


  def test_resolve_data_root_precedence(tmp_path, monkeypatch):
      cli_root = tmp_path / "cli"
      env_root = tmp_path / "env"
      monkeypatch.setenv("LM_DATA_ROOT", str(env_root))
      # --root wins over env
      assert cfg.resolve_data_root(str(cli_root)) == cli_root
      # env wins over default when no --root
      assert cfg.resolve_data_root(None) == env_root
      # default when neither
      monkeypatch.delenv("LM_DATA_ROOT", raising=False)
      assert cfg.resolve_data_root(None) == (Path.home() / ".lean_memory")


  def test_resolve_data_root_expanduser(monkeypatch):
      monkeypatch.setenv("LM_DATA_ROOT", "~/somewhere")
      assert cfg.resolve_data_root(None) == (Path.home() / "somewhere")


  def test_load_config_docker_requires_api_key(monkeypatch, tmp_path):
      monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
      monkeypatch.delenv("LM_API_KEY", raising=False)
      with pytest.raises(SystemExit) as exc:
          cfg.load_config("docker")
      assert exc.value.code == 2


  def test_load_config_docker_with_api_key(monkeypatch, tmp_path):
      monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
      monkeypatch.setenv("LM_API_KEY", "secret-key")
      c = cfg.load_config("docker")
      assert c.mode == "docker"
      assert c.api_key == "secret-key"
      assert c.session_token is None
      assert c.data_root == tmp_path


  def test_load_config_local_generates_session_token(monkeypatch, tmp_path):
      monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
      monkeypatch.delenv("LM_API_KEY", raising=False)
      c = cfg.load_config("local")
      assert c.mode == "local"
      assert c.api_key is None
      assert isinstance(c.session_token, str) and len(c.session_token) >= 24
      # two loads => different tokens
      c2 = cfg.load_config("local")
      assert c.session_token != c2.session_token


  def test_load_config_models_from_env(monkeypatch, tmp_path):
      monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
      monkeypatch.setenv("LM_CONSOLE_MODELS", "stub")
      c = cfg.load_config("local")
      assert c.models == "stub"


  from pathlib import Path  # noqa: E402  (kept last so the fixtures above read top-down)
  ```

- [ ] **Step 6: Run the test — Expected: FAIL.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_config.py -v
  ```
  Expected: FAIL with `ModuleNotFoundError: No module named
  'lean_memory_console.config'` (config.py not written yet).

- [ ] **Step 7: Write `console/src/lean_memory_console/config.py`.**
  ```python
  """Console configuration: data-root resolution, namespace sanitizer mirror,
  reserved-namespace guard, and mode-specific config loading (spec §5, §7, §10).

  SAFE_NS_RE and sanitize_namespace mirror the engine's private sanitizer
  (memory.py:38, 70-71). The mirror is guarded by the fail-loud tripwire in
  inspect_sql.py (spec §13) so engine drift turns the suite red.
  """

  from __future__ import annotations

  import os
  import re
  import secrets
  import sys
  from dataclasses import dataclass
  from pathlib import Path

  # Mirror of engine memory.py:38  _SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")
  SAFE_NS_RE: re.Pattern = re.compile(r"[^A-Za-z0-9_.-]")

  DEFAULT_DATA_ROOT = Path("~/.lean_memory")
  DEFAULT_PORT = 8377


  def sanitize_namespace(name: str) -> str:
      """Mirror of memory.py:70-71  safe = _SAFE_NS.sub("_", name) or "default"."""
      return SAFE_NS_RE.sub("_", name) or "default"


  def is_reserved_namespace(name: str) -> bool:
      """True when the sanitized namespace begins with '_' (collides with sidecars
      like _events.db); the data plane rejects these (spec §5)."""
      return sanitize_namespace(name).startswith("_")


  def ns_db_path(data_root: Path, namespace: str) -> Path:
      """The engine store file for a namespace: root / f'{safe}.db'."""
      return data_root / f"{sanitize_namespace(namespace)}.db"


  def resolve_data_root(cli_root: str | None) -> Path:
      """One rule, both commands (spec §10): --root > LM_DATA_ROOT > ~/.lean_memory.
      expanduser is applied; this function never creates the directory."""
      if cli_root:
          return Path(cli_root).expanduser()
      env = os.environ.get("LM_DATA_ROOT")
      if env:
          return Path(env).expanduser()
      return DEFAULT_DATA_ROOT.expanduser()


  @dataclass
  class ConsoleConfig:
      data_root: Path
      mode: str  # "local" | "docker"
      api_key: str | None = None
      port: int = DEFAULT_PORT
      models: str = "auto"  # "auto" | "stub"
      session_token: str | None = None


  def load_config(
      mode: str, cli_root: str | None = None, port: int | None = None
  ) -> ConsoleConfig:
      """Build a ConsoleConfig for the given mode.

      docker: LM_API_KEY is required — a missing key raises SystemExit(2) with a
              clear message. No per-launch session token.
      local:  a fresh random session_token is minted (embedded in the tokened URL).
      """
      data_root = resolve_data_root(cli_root)
      models = os.environ.get("LM_CONSOLE_MODELS", "auto")
      if models not in ("auto", "stub"):
          models = "auto"
      resolved_port = port if port is not None else DEFAULT_PORT

      if mode == "docker":
          api_key = os.environ.get("LM_API_KEY")
          if not api_key:
              print(
                  "LM_API_KEY is required in Docker mode; refusing to boot.",
                  file=sys.stderr,
              )
              raise SystemExit(2)
          return ConsoleConfig(
              data_root=data_root,
              mode="docker",
              api_key=api_key,
              port=resolved_port,
              models=models,
              session_token=None,
          )

      # local mode
      return ConsoleConfig(
          data_root=data_root,
          mode="local",
          api_key=None,
          port=resolved_port,
          models=models,
          session_token=secrets.token_urlsafe(24),
      )
  ```

- [ ] **Step 8: Run the test — Expected: PASS.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_config.py -v
  ```
  Expected: PASS (all config tests green).

- [ ] **Step 9: Write a minimal `console/README.md`.**
  ```markdown
  # lean-memory-console

  Agent-first, read-only verification console for
  [lean-memory](../README.md). Agents write and search memory; the human opens
  the console to verify what was stored.

  ## Develop

      python3 -m venv console/.venv
      console/.venv/bin/pip install -e . -e './console[dev]'
      console/.venv/bin/python -m pytest console/tests -v

  Runs fully offline on stub backends (`LM_CONSOLE_MODELS=stub`).
  ```

- [ ] **Step 10: Commit.**
  ```bash
  git add console/pyproject.toml console/README.md \
          console/src/lean_memory_console/__init__.py \
          console/src/lean_memory_console/config.py \
          console/tests/__init__.py console/tests/test_config.py \
          deploy/docker-compose.yml
  git commit -m "feat(console): package scaffold + config (data-root, ns sanitizer, load_config)"
  ```

---

### Task 2: `events.py` — the `_events.db` sidecar

**Files:**
- Create: `console/src/lean_memory_console/events.py`
- Test: `console/tests/test_events.py`

**Interfaces:**
- Consumes: `ConsoleConfig.data_root` (a `Path`).
- Produces: `CAP`, `EventLog` with `record`, `list_events`, `activity_summary`,
  `close`.

- [ ] **Step 1: Write the failing test `console/tests/test_events.py`.**
  ```python
  import sqlite3
  import threading

  import pytest

  from lean_memory_console.events import CAP, EventLog


  def _score_payload():
      return {
          "query": "where does the user work?",
          "k": 5,
          "latest_only": True,
          "origin": "agent",
          "hits": [
              {
                  "fact_id": "f1",
                  "fact_text": "The user works at Acme.",
                  "final_score": 0.82,
                  "relevance": 0.9,
                  "recency": 0.7,
                  "importance": 0.5,
                  "dense_rank": 1,
                  "sparse_rank": 2,
                  "rrf_score": 0.031,
              }
          ],
      }


  def test_schema_created(tmp_path):
      log = EventLog(tmp_path)
      try:
          db = sqlite3.connect(tmp_path / "_events.db")
          cols = {r[1] for r in db.execute("PRAGMA table_info(event)").fetchall()}
          assert cols == {
              "id", "namespace", "ts", "kind", "duration_ms", "payload"
          }
          idx = {r[1] for r in db.execute("PRAGMA index_list(event)").fetchall()}
          assert "ix_event_ns_ts" in idx
          db.close()
      finally:
          log.close()


  def test_record_and_list_roundtrip_decodes_payload(tmp_path):
      log = EventLog(tmp_path)
      try:
          payload = _score_payload()
          log.record("ns1", "search", 12.5, payload)
          out = log.list_events("ns1")
          assert out["total"] == 1
          assert out["page"] == 1 and out["page_size"] == 50
          item = out["items"][0]
          assert item["namespace"] == "ns1"
          assert item["kind"] == "search"
          assert item["duration_ms"] == 12.5
          assert item["payload"] == payload  # JSON round-trips, decoded to dict
          assert isinstance(item["ts"], int)
      finally:
          log.close()


  def test_kind_filter(tmp_path):
      log = EventLog(tmp_path)
      try:
          log.record("ns1", "add", 1.0, {"fact_count": 2})
          log.record("ns1", "search", 2.0, _score_payload())
          assert log.list_events("ns1", kind="add")["total"] == 1
          assert log.list_events("ns1", kind="search")["total"] == 1
          assert log.list_events("ns1")["total"] == 2
      finally:
          log.close()


  def test_list_ordered_ts_desc_then_id_desc(tmp_path):
      log = EventLog(tmp_path)
      try:
          for i in range(3):
              log.record("ns1", "add", float(i), {"n": i})
          items = log.list_events("ns1")["items"]
          # newest first: id DESC breaks ties on identical ts
          ids = [it["id"] for it in items]
          assert ids == sorted(ids, reverse=True)
      finally:
          log.close()


  def test_activity_summary_excludes_ui_origin(tmp_path):
      log = EventLog(tmp_path)
      try:
          log.record("ns1", "add", 1.0, {"source": "user"})
          log.record("ns1", "search", 1.0, {"origin": "agent"})
          log.record("ns1", "search", 1.0, {"origin": "ui"})  # excluded
          summ = log.activity_summary("ns1")
          assert summ["adds"] == 1
          assert summ["searches"] == 1  # the ui search is excluded
          assert isinstance(summ["earliest_ts"], int)
      finally:
          log.close()


  def test_activity_summary_empty_earliest_none(tmp_path):
      log = EventLog(tmp_path)
      try:
          summ = log.activity_summary("nobody")
          assert summ == {"adds": 0, "searches": 0, "earliest_ts": None}
      finally:
          log.close()


  def test_retention_prunes_to_cap_keeping_newest(tmp_path):
      log = EventLog(tmp_path)
      try:
          for i in range(CAP + 1):  # 10_001 events
              log.record("ns1", "add", float(i), {"n": i})
          out = log.list_events("ns1", page_size=1)
          assert out["total"] == CAP  # pruned to exactly 10_000
          # the surviving newest carries the last payload
          assert out["items"][0]["payload"]["n"] == CAP
          # the oldest (n == 0) is gone
          db = sqlite3.connect(tmp_path / "_events.db")
          gone = db.execute(
              "SELECT COUNT(*) FROM event WHERE namespace=? AND payload LIKE ?",
              ("ns1", '%"n": 0%'),
          ).fetchone()[0]
          db.close()
          assert gone == 0
      finally:
          log.close()


  def test_record_never_raises(tmp_path, monkeypatch, caplog):
      log = EventLog(tmp_path)
      try:
          # Force the write path to throw; record must swallow + log + return None.
          def boom(*a, **k):
              raise sqlite3.OperationalError("disk I/O error")

          monkeypatch.setattr(log._db, "execute", boom)
          import logging

          with caplog.at_level(logging.WARNING, logger="lean_memory_console.events"):
              assert log.record("ns1", "add", 1.0, {"x": 1}) is None
          assert any("lean_memory_console.events" == r.name for r in caplog.records)
      finally:
          log.close()


  def test_concurrent_records_lose_nothing(tmp_path):
      log = EventLog(tmp_path)
      try:
          n_threads, per = 4, 50

          def worker(t):
              for i in range(per):
                  log.record("ns1", "add", float(i), {"t": t, "i": i})

          threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
          for th in threads:
              th.start()
          for th in threads:
              th.join()
          assert log.list_events("ns1", page_size=1)["total"] == n_threads * per
      finally:
          log.close()
  ```

- [ ] **Step 2: Run the test — Expected: FAIL.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_events.py -v
  ```
  Expected: FAIL with `ModuleNotFoundError: No module named
  'lean_memory_console.events'`.

- [ ] **Step 3: Write `console/src/lean_memory_console/events.py`.**
  ```python
  """The _events.db sidecar (spec §5): schema, event recording, atomic retention,
  and read helpers. Console-owned (the engine never touches this file).

  All connections set busy_timeout=5000 because the engine sets none (spec §6).
  record() NEVER raises — recording an event must not mask the operation's own
  result; on any failure it degrades to a logged warning and returns None.
  """

  from __future__ import annotations

  import json
  import logging
  import sqlite3
  import threading
  import time
  from pathlib import Path

  CAP = 10_000

  _log = logging.getLogger("lean_memory_console.events")

  _SCHEMA = """
  CREATE TABLE IF NOT EXISTS event (
      id          INTEGER PRIMARY KEY,
      namespace   TEXT,
      ts          INTEGER,
      kind        TEXT CHECK(kind IN ('add','search')),
      duration_ms REAL,
      payload     TEXT
  );
  CREATE INDEX IF NOT EXISTS ix_event_ns_ts ON event(namespace, ts);
  """

  _PRUNE_SQL = (
      "DELETE FROM event WHERE namespace=? AND id NOT IN ("
      "SELECT id FROM event WHERE namespace=? ORDER BY ts DESC, id DESC LIMIT 10000)"
  )


  class EventLog:
      def __init__(self, data_root: Path) -> None:
          self.path = Path(data_root) / "_events.db"
          self._lock = threading.Lock()
          self._db = sqlite3.connect(str(self.path), check_same_thread=False)
          self._db.row_factory = sqlite3.Row
          self._db.execute("PRAGMA journal_mode=WAL")
          self._db.execute("PRAGMA busy_timeout=5000")
          self._db.execute("PRAGMA foreign_keys=ON")
          self._db.executescript(_SCHEMA)
          self._db.commit()

      def record(self, namespace: str, kind: str, duration_ms: float, payload: dict) -> None:
          """Insert one event, then prune if this namespace is over CAP. Never raises."""
          try:
              ts = int(time.time() * 1000)
              blob = json.dumps(payload)
              with self._lock:
                  self._db.execute(
                      "INSERT INTO event(namespace, ts, kind, duration_ms, payload) "
                      "VALUES (?,?,?,?,?)",
                      (namespace, ts, kind, duration_ms, blob),
                  )
                  count = self._db.execute(
                      "SELECT COUNT(*) FROM event WHERE namespace=?", (namespace,)
                  ).fetchone()[0]
                  if count > CAP:
                      self._db.execute(_PRUNE_SQL, (namespace, namespace))
                  self._db.commit()
          except Exception as exc:  # noqa: BLE001 — must never mask the caller's result
              _log.warning("event record failed (%s): %s", kind, exc)
              return None
          return None

      def list_events(
          self, namespace: str, kind: str | None = None, page: int = 1, page_size: int = 50
      ) -> dict:
          page = max(page, 1)
          page_size = min(max(page_size, 1), 200)
          where = "WHERE namespace=?"
          args: list = [namespace]
          if kind is not None:
              where += " AND kind=?"
              args.append(kind)
          with self._lock:
              total = self._db.execute(
                  f"SELECT COUNT(*) FROM event {where}", args
              ).fetchone()[0]
              rows = self._db.execute(
                  f"SELECT id, namespace, ts, kind, duration_ms, payload FROM event "
                  f"{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                  (*args, page_size, (page - 1) * page_size),
              ).fetchall()
          items = []
          for r in rows:
              items.append(
                  {
                      "id": r["id"],
                      "namespace": r["namespace"],
                      "ts": r["ts"],
                      "kind": r["kind"],
                      "duration_ms": r["duration_ms"],
                      "payload": json.loads(r["payload"]) if r["payload"] else {},
                  }
              )
          return {"items": items, "page": page, "page_size": page_size, "total": total}

      def activity_summary(self, namespace: str, days: int = 7) -> dict:
          """Adds/searches in the window, EXCLUDING payload origin == 'ui' (spec §7).
          The window bound is applied via ts; earliest_ts is over all stored events."""
          since = int(time.time() * 1000) - days * 86_400_000
          with self._lock:
              rows = self._db.execute(
                  "SELECT kind, payload FROM event WHERE namespace=? AND ts >= ?",
                  (namespace, since),
              ).fetchall()
              earliest = self._db.execute(
                  "SELECT MIN(ts) FROM event WHERE namespace=?", (namespace,)
              ).fetchone()[0]
          adds = 0
          searches = 0
          for r in rows:
              payload = json.loads(r["payload"]) if r["payload"] else {}
              if payload.get("origin") == "ui":
                  continue
              if r["kind"] == "add":
                  adds += 1
              elif r["kind"] == "search":
                  searches += 1
          return {"adds": adds, "searches": searches, "earliest_ts": earliest}

      def close(self) -> None:
          with self._lock:
              self._db.close()
  ```

- [ ] **Step 4: Run the test — Expected: PASS.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_events.py -v
  ```
  Expected: PASS (schema, round-trip, filter, ordering, activity exclusion,
  retention boundary, never-raises, concurrency all green).

- [ ] **Step 5: Commit.**
  ```bash
  git add console/src/lean_memory_console/events.py console/tests/test_events.py
  git commit -m "feat(console): _events.db sidecar — record, list, activity, atomic retention"
  ```

---

### Task 3: fixture builder + `inspect_sql.py` part 1 (enumeration + tripwires)

**Files:**
- Create: `console/tests/fixtures/build_fixture.py`
- Create: `console/tests/fixtures/data_root/` (committed output of the builder)
- Create: `console/src/lean_memory_console/inspect_sql.py`
- Test: `console/tests/test_inspect_namespaces.py`

**Interfaces:**
- Consumes: `lean_memory.Memory` (default stub backends), `EventLog`,
  `sanitize_namespace`.
- Produces: `open_ro`, `list_namespaces`, `compute_engine_schema_fingerprint`,
  `compute_sanitizer_fingerprint`, `EXPECTED_SCHEMA_FINGERPRINT`,
  `EXPECTED_SANITIZER_FINGERPRINT`. (Task 4 adds the fact/episode/entity readers
  to the same module.)

- [ ] **Step 1: Write the fixture builder `console/tests/fixtures/build_fixture.py`.**
  Deterministic (stub backends via bare `Memory(root=...)`; the stub extractor
  turns each add into facts over the utterance). Spec §12 contents: 2 namespaces,
  2 episodes each, ≥1 supersession chain length ≥2, ≥1 entity with 2 facts, plus
  three `_events.db` rows (1 add with `superseded_count>0`, 1 search with a full
  §5 score payload, 1 event with `payload.error`).
  ```python
  """Deterministic fixture builder for the console read-path tests (spec §12).

  Uses lean_memory.Memory with its default OFFLINE stub backends (no overrides =
  FakeEmbedder + StubCandidateGenerator + StubTyper + ...). Output is committed
  under console/tests/fixtures/data_root/ and is the acceptance criteria:
    - 2 namespaces (proj-alpha, proj-beta)
    - 2 episodes each
    - >=1 supersession chain of length >=2 (one retired + one latest)
    - >=1 entity with 2 facts
    - _events.db: 1 add event (superseded_count>0), 1 search event (full score
      payload), 1 event with payload.error
  Rebuild + re-commit whenever the mirrored engine schema changes.
  """

  from __future__ import annotations

  import shutil
  import sqlite3
  from pathlib import Path

  from lean_memory import Memory

  from lean_memory_console.events import EventLog

  FIXTURE_DIR = Path(__file__).resolve().parent / "data_root"

  # Fixed epoch-ms reference times so the fixture is byte-stable across builds.
  T0 = 1_700_000_000_000
  DAY = 86_400_000


  def _facts_of(mem: Memory, namespace: str) -> list[tuple]:
      path = mem.root / f"{namespace}.db"
      db = sqlite3.connect(path)
      db.row_factory = sqlite3.Row
      rows = db.execute(
          "SELECT id, subject_id, predicate, is_latest, superseded_by "
          "FROM fact ORDER BY created_at, id"
      ).fetchall()
      db.close()
      return rows


  def build(target: Path = FIXTURE_DIR) -> Path:
      if target.exists():
          shutil.rmtree(target)
      target.mkdir(parents=True, exist_ok=True)

      mem = Memory(root=str(target))
      try:
          # ── proj-alpha: 2 episodes; a supersession chain on a repeated slot,
          #    and an entity ("Ada") mentioned in both episodes (2+ facts). ──
          add1 = mem.add(
              "proj-alpha", "Ada works at Acme.", t_ref=T0, source="user"
          )
          add2 = mem.add(
              "proj-alpha", "Ada works at Globex now.", t_ref=T0 + DAY, source="user"
          )

          # ── proj-beta: 2 episodes, plain facts (no forced supersession). ──
          mem.add("proj-beta", "The project ships on Friday.", t_ref=T0, source="user")
          mem.add(
              "proj-beta", "The demo is scheduled for Monday.",
              t_ref=T0 + DAY, source="user",
          )
      finally:
          mem.close()

      # Determine which of proj-alpha's adds produced a supersession, so the add
      # event payload carries a real superseded id.
      alpha_rows = _facts_of(Memory(root=str(target)), "proj-alpha")
      retired = [r for r in alpha_rows if r["superseded_by"] is not None]
      latest_add_ids = list(add2)
      superseded_ids = [r["id"] for r in retired]

      # ── events sidecar: the three required rows (spec §12). ──
      log = EventLog(target)
      try:
          log.record(
              "proj-alpha",
              "add",
              7.5,
              {
                  "episode_text_chars": len("Ada works at Globex now."),
                  "source": "user",
                  "t_ref": T0 + DAY,
                  "fact_ids": latest_add_ids,
                  "fact_count": len(latest_add_ids),
                  "superseded_fact_ids": superseded_ids,
                  "superseded_count": len(superseded_ids),
                  "origin": "agent",
              },
          )
          log.record(
              "proj-alpha",
              "search",
              4.2,
              {
                  "query": "where does Ada work?",
                  "k": 5,
                  "latest_only": True,
                  "origin": "agent",
                  "hits": [
                      {
                          "fact_id": latest_add_ids[0] if latest_add_ids else "f1",
                          "fact_text": "Ada works at Globex now.",
                          "final_score": 0.81,
                          "relevance": 0.88,
                          "recency": 0.72,
                          "importance": 0.40,
                          "dense_rank": 1,
                          "sparse_rank": 1,
                          "rrf_score": 0.032,
                      }
                  ],
              },
          )
          log.record(
              "proj-alpha",
              "search",
              0.0,
              {
                  "query": "bad query",
                  "k": 5,
                  "latest_only": True,
                  "origin": "agent",
                  "error": "engine raised: simulated failure",
              },
          )
      finally:
          log.close()

      assert superseded_ids, "fixture must contain >=1 supersession chain"
      return target


  if __name__ == "__main__":
      out = build()
      print(f"fixture built at {out}")
  ```

- [ ] **Step 2: Build and commit the fixture data root.**
  ```bash
  console/.venv/bin/python -m tests.fixtures.build_fixture
  ```
  Run from `console/` OR invoke by path:
  ```bash
  cd console && ./.venv/bin/python -c "from tests.fixtures.build_fixture import build; print(build())"; cd ..
  ```
  Expected: prints `fixture built at .../console/tests/fixtures/data_root`;
  the dir now contains `proj-alpha.db`, `proj-beta.db`, `_events.db` (+ `-wal`/
  `-shm` sidecars). The committed fixture keeps the main `.db` files; do NOT
  commit `-wal`/`-shm` (see Step 8).

- [ ] **Step 3: Write `inspect_sql.py` part 1 (this task's surface only).**
  ```python
  """Read-only enumeration SQL over the engine's per-namespace DBs (spec §7),
  plus the fail-loud schema/sanitizer tripwires (spec §13).

  Connections open with file:...?mode=ro ALWAYS; immutable=1 is a per-request
  fallback tried ONLY after mode=ro raises OperationalError (spec §7). Column
  names below are verbatim from the installed lean_memory store/schema.py.

  Task 4 extends this module with list_facts/get_fact/list_episodes/
  get_episode/list_entities.
  """

  from __future__ import annotations

  import hashlib
  import importlib.resources
  import sqlite3
  from pathlib import Path

  from .config import sanitize_namespace


  def open_ro(path: Path) -> sqlite3.Connection:
      """Read-only engine connection. mode=ro first; immutable=1 only on
      OperationalError (genuinely read-only media / error-14 path, spec §7)."""
      uri = f"file:{path}?mode=ro"
      try:
          conn = sqlite3.connect(uri, uri=True)
      except sqlite3.OperationalError:
          conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
      conn.row_factory = sqlite3.Row
      conn.execute("PRAGMA busy_timeout=5000")
      return conn


  def list_namespaces(data_root: Path, event_log) -> list[dict]:
      """Discover *.db (skipping _*.db), returning per-namespace counts.
      Bare array (unpaginated), ordered by total facts DESC then name (spec §7)."""
      data_root = Path(data_root)
      out: list[dict] = []
      for db_path in sorted(data_root.glob("*.db")):
          if db_path.name.startswith("_"):
              continue
          name = db_path.stem
          conn = open_ro(db_path)
          try:
              facts_latest = conn.execute(
                  "SELECT COUNT(*) FROM fact WHERE is_latest=1"
              ).fetchone()[0]
              facts_retired = conn.execute(
                  "SELECT COUNT(*) FROM fact WHERE is_latest=0"
              ).fetchone()[0]
              entities = conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
              episodes = conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
              chains = conn.execute(
                  "SELECT COUNT(*) FROM fact WHERE superseded_by IS NOT NULL"
              ).fetchone()[0]
              top_predicates = [
                  {"predicate": r["predicate"], "count": r["n"]}
                  for r in conn.execute(
                      "SELECT predicate, COUNT(*) AS n FROM fact "
                      "GROUP BY predicate ORDER BY n DESC, predicate LIMIT 5"
                  ).fetchall()
              ]
          finally:
              conn.close()
          file_size = db_path.stat().st_size
          activity = event_log.activity_summary(name)
          out.append(
              {
                  "name": name,
                  "facts_latest": facts_latest,
                  "facts_retired": facts_retired,
                  "entities": entities,
                  "episodes": episodes,
                  "chains": chains,
                  "file_size": file_size,
                  "top_predicates": top_predicates,
                  "activity": activity,
              }
          )
      out.sort(key=lambda n: (-(n["facts_latest"] + n["facts_retired"]), n["name"]))
      return out


  def _digest_lines(text: str, predicate) -> str:
      lines = [ln for ln in text.splitlines() if predicate(ln)]
      return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


  def compute_engine_schema_fingerprint() -> str:
      """sha256 over the CREATE lines of the INSTALLED lean_memory store/schema.py
      (importlib.resources, never a checked-in copy) — the §13 tripwire."""
      text = (
          importlib.resources.files("lean_memory.store")
          .joinpath("schema.py")
          .read_text(encoding="utf-8")
      )
      return _digest_lines(text, lambda ln: "create" in ln.lower())


  def compute_sanitizer_fingerprint() -> str:
      """sha256 over memory.py's sanitizer lines (_SAFE_NS / the 'or "default"'
      fallback) — guards the config.py mirror against engine drift (§13)."""
      text = (
          importlib.resources.files("lean_memory")
          .joinpath("memory.py")
          .read_text(encoding="utf-8")
      )
      return _digest_lines(
          text, lambda ln: "_SAFE_NS" in ln or 'or "default"' in ln
      )


  # Filled once from the first run's printed digests (Step 5), then a test pins
  # equality so engine drift turns the suite red.
  EXPECTED_SCHEMA_FINGERPRINT = "REPLACE_WITH_COMPUTED_SCHEMA_DIGEST"
  EXPECTED_SANITIZER_FINGERPRINT = "REPLACE_WITH_COMPUTED_SANITIZER_DIGEST"
  ```

- [ ] **Step 4: Write the failing test `console/tests/test_inspect_namespaces.py`.**
  ```python
  import shutil
  import sqlite3

  import pytest

  from lean_memory_console import inspect_sql
  from lean_memory_console.events import EventLog

  from tests.fixtures.build_fixture import FIXTURE_DIR


  def _copy_fixture(tmp_path):
      dst = tmp_path / "data_root"
      shutil.copytree(FIXTURE_DIR, dst)
      return dst


  def test_open_ro_reads_fixture_db(tmp_path):
      root = _copy_fixture(tmp_path)
      conn = inspect_sql.open_ro(root / "proj-alpha.db")
      try:
          n = conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
          assert n > 0
      finally:
          conn.close()


  def test_open_ro_reads_without_wal_sidecar(tmp_path):
      root = _copy_fixture(tmp_path)
      # delete any -wal/-shm sidecars — mode=ro must still read the base file
      for suffix in ("-wal", "-shm"):
          p = root / f"proj-alpha.db{suffix}"
          if p.exists():
              p.unlink()
      conn = inspect_sql.open_ro(root / "proj-alpha.db")
      try:
          assert conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0] > 0
      finally:
          conn.close()


  def test_open_ro_immutable_fallback_only_after_operationalerror(tmp_path, monkeypatch):
      root = _copy_fixture(tmp_path)
      calls = []
      real_connect = sqlite3.connect

      def fake_connect(uri, *a, **k):
          calls.append(uri)
          if len(calls) == 1:
              raise sqlite3.OperationalError("unable to open database file")
          return real_connect(uri, *a, **k)

      monkeypatch.setattr(sqlite3, "connect", fake_connect)
      conn = inspect_sql.open_ro(root / "proj-alpha.db")
      try:
          # first attempt mode=ro (raised), second attempt immutable=1 (succeeded)
          assert "mode=ro" in calls[0]
          assert "immutable=1" in calls[1]
          assert conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0] > 0
      finally:
          conn.close()


  def test_list_namespaces_skips_events_and_counts(tmp_path):
      root = _copy_fixture(tmp_path)
      log = EventLog(root)
      try:
          nss = inspect_sql.list_namespaces(root, log)
      finally:
          log.close()
      names = [n["name"] for n in nss]
      assert "_events" not in names
      assert set(names) == {"proj-alpha", "proj-beta"}
      alpha = next(n for n in nss if n["name"] == "proj-alpha")
      assert alpha["episodes"] == 2
      assert alpha["chains"] >= 1
      assert alpha["facts_retired"] >= 1
      assert alpha["facts_latest"] >= 1
      assert isinstance(alpha["top_predicates"], list)
      assert alpha["activity"]["adds"] >= 0  # activity envelope present


  def test_fingerprints_match_expected():
      assert (
          inspect_sql.compute_engine_schema_fingerprint()
          == inspect_sql.EXPECTED_SCHEMA_FINGERPRINT
      )
      assert (
          inspect_sql.compute_sanitizer_fingerprint()
          == inspect_sql.EXPECTED_SANITIZER_FINGERPRINT
      )
  ```

- [ ] **Step 5: Run — Expected: FAIL on the fingerprint test only; print the real digests.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_inspect_namespaces.py -v
  ```
  Expected: the four enumeration tests PASS; `test_fingerprints_match_expected`
  FAILS (`REPLACE_WITH_...` != real digest). Now print the true digests:
  ```bash
  console/.venv/bin/python -c "from lean_memory_console import inspect_sql as i; print('SCHEMA', i.compute_engine_schema_fingerprint()); print('SANITIZER', i.compute_sanitizer_fingerprint())"
  ```

- [ ] **Step 6: Paste the two printed digests into the constants.**
  Edit `console/src/lean_memory_console/inspect_sql.py`, replacing
  `EXPECTED_SCHEMA_FINGERPRINT` and `EXPECTED_SANITIZER_FINGERPRINT` with the
  exact hex strings printed in Step 5.

- [ ] **Step 7: Re-run — Expected: PASS.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_inspect_namespaces.py -v
  ```
  Expected: all tests PASS (fingerprints now match).

- [ ] **Step 8: Ensure WAL/shm sidecars are not committed; commit fixture + code.**
  Add a fixture-scoped gitignore so only the base `.db` files are tracked:
  ```bash
  printf '*.db-wal\n*.db-shm\n*-wal\n*-shm\n' > console/tests/fixtures/data_root/.gitignore
  git add console/tests/fixtures/build_fixture.py \
          console/tests/fixtures/data_root/.gitignore \
          console/tests/fixtures/data_root/proj-alpha.db \
          console/tests/fixtures/data_root/proj-beta.db \
          console/tests/fixtures/data_root/_events.db \
          console/src/lean_memory_console/inspect_sql.py \
          console/tests/test_inspect_namespaces.py
  git commit -m "feat(console): fixture builder + inspect_sql enumeration & schema tripwires"
  ```

---

### Task 4: `inspect_sql.py` part 2 — facts, chain walk, episodes, entities

**Files:**
- Modify: `console/src/lean_memory_console/inspect_sql.py`
- Test: `console/tests/test_inspect_facts.py`

**Interfaces:**
- Consumes: the fixture `data_root`, `open_ro`.
- Produces: `list_facts`, `get_fact`, `list_episodes`, `get_episode`,
  `list_entities` (all envelope-shaped except `get_*` which return a single dict
  or None).

- [ ] **Step 1: Write the failing test `console/tests/test_inspect_facts.py`.**
  ```python
  import shutil

  import pytest

  from lean_memory_console import inspect_sql

  from tests.fixtures.build_fixture import FIXTURE_DIR


  @pytest.fixture()
  def alpha_db(tmp_path):
      dst = tmp_path / "data_root"
      shutil.copytree(FIXTURE_DIR, dst)
      return dst / "proj-alpha.db"


  def test_list_facts_latest_only_default(alpha_db):
      out = inspect_sql.list_facts(alpha_db)
      assert out["page"] == 1 and out["page_size"] == 50
      # default latest_only=True: no retired facts in the page
      assert out["total"] >= 1
      assert all(row["is_latest"] == 1 for row in out["items"])


  def test_list_facts_includes_retired_when_flag_off(alpha_db):
      latest = inspect_sql.list_facts(alpha_db, latest_only=True)["total"]
      allf = inspect_sql.list_facts(alpha_db, latest_only=False)["total"]
      assert allf > latest  # retired chain member now visible


  def test_list_facts_carries_subject_name(alpha_db):
      out = inspect_sql.list_facts(alpha_db, latest_only=False)
      # every fact row exposes the joined entity name as "subject"
      assert all("subject" in row for row in out["items"])
      assert any(row["subject"] == "Ada" for row in out["items"])


  def test_list_facts_predicate_exact(alpha_db):
      allf = inspect_sql.list_facts(alpha_db, latest_only=False)
      pred = allf["items"][0]["predicate"]
      filtered = inspect_sql.list_facts(alpha_db, latest_only=False, predicate=pred)
      assert filtered["total"] >= 1
      assert all(r["predicate"] == pred for r in filtered["items"])


  def test_list_facts_entity_case_insensitive(alpha_db):
      lower = inspect_sql.list_facts(alpha_db, latest_only=False, entity="ada")
      upper = inspect_sql.list_facts(alpha_db, latest_only=False, entity="ADA")
      assert lower["total"] == upper["total"] >= 2  # Ada has >=2 facts


  def test_list_facts_min_salience(alpha_db):
      allf = inspect_sql.list_facts(alpha_db, latest_only=False)
      hi = max(r["salience"] for r in allf["items"])
      filtered = inspect_sql.list_facts(alpha_db, latest_only=False, min_salience=hi)
      assert filtered["total"] >= 1
      assert all(r["salience"] >= hi for r in filtered["items"])


  def test_list_facts_q_fts_match(alpha_db):
      out = inspect_sql.list_facts(alpha_db, latest_only=False, q="Globex")
      assert out["total"] >= 1
      assert all("globex" in r["fact_text"].lower() for r in out["items"])


  def test_list_facts_envelope_total_is_post_filter(alpha_db):
      allf = inspect_sql.list_facts(alpha_db, latest_only=False)
      one = inspect_sql.list_facts(alpha_db, latest_only=False, q="Globex")
      assert one["total"] <= allf["total"]


  def test_get_fact_chain_oldest_to_newest(alpha_db):
      # find the retired fact (has superseded_by) then fetch the latest head
      allf = inspect_sql.list_facts(alpha_db, latest_only=False)
      head = next(r for r in allf["items"] if r["is_latest"] == 1 and r["subject"] == "Ada")
      got = inspect_sql.get_fact(alpha_db, head["id"])
      assert got is not None
      chain = got["chain"]
      assert len(chain) >= 2
      # oldest -> newest ordering: last is the latest, first is retired
      assert chain[-1]["is_latest"] == 1
      assert chain[0]["is_latest"] == 0
      assert got["episode"] is not None
      assert got["episode"]["id"] == got["episode_id"]


  def test_get_fact_missing_returns_none(alpha_db):
      assert inspect_sql.get_fact(alpha_db, "does-not-exist") is None


  def test_list_episodes_order_and_facts(alpha_db):
      out = inspect_sql.list_episodes(alpha_db)
      assert out["total"] == 2
      trefs = [e["t_ref"] for e in out["items"]]
      assert trefs == sorted(trefs, reverse=True)  # t_ref DESC


  def test_get_episode_carries_its_facts(alpha_db):
      eps = inspect_sql.list_episodes(alpha_db)["items"]
      ep = inspect_sql.get_episode(alpha_db, eps[0]["id"])
      assert ep is not None
      assert "facts" in ep
      assert all(f["episode_id"] == ep["id"] for f in ep["facts"])


  def test_list_entities_fact_count(alpha_db):
      out = inspect_sql.list_entities(alpha_db)
      assert out["total"] >= 1
      ada = next(e for e in out["items"] if e["name"] == "Ada")
      assert ada["fact_count"] >= 2
      # ordered fact_count DESC then name
      counts = [e["fact_count"] for e in out["items"]]
      assert counts == sorted(counts, reverse=True)
  ```

- [ ] **Step 2: Run — Expected: FAIL.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_inspect_facts.py -v
  ```
  Expected: FAIL with `AttributeError: module 'lean_memory_console.inspect_sql'
  has no attribute 'list_facts'`.

- [ ] **Step 3: Append the part-2 readers to `inspect_sql.py`.**
  Insert BEFORE the `EXPECTED_*` constants block (constants stay at module end).
  Every column name matches `store/schema.py` verbatim.
  ```python
  def _paginate(page: int, page_size: int) -> tuple[int, int, int]:
      page = max(page, 1)
      page_size = min(max(page_size, 1), 200)
      return page, page_size, (page - 1) * page_size


  def list_facts(
      db_path: Path,
      latest_only: bool = True,
      predicate: str | None = None,
      entity: str | None = None,
      min_salience: float | None = None,
      q: str | None = None,
      page: int = 1,
      page_size: int = 50,
  ) -> dict:
      """Filterable fact list. Rows carry subject = entity.name (joined via
      fact.subject_id). q is an FTS filter over fact_fts (distinct from search).
      Order created_at DESC, id DESC; total is post-filter (spec §7)."""
      page, page_size, offset = _paginate(page, page_size)
      where = ["1=1"]
      args: list = []
      if latest_only:
          where.append("f.is_latest = 1")
      if predicate is not None:
          where.append("f.predicate = ?")
          args.append(predicate)
      if entity is not None:
          where.append("LOWER(e.name) = LOWER(?)")
          args.append(entity)
      if min_salience is not None:
          where.append("f.salience >= ?")
          args.append(min_salience)
      if q is not None:
          where.append(
              "f.id IN (SELECT fact_id FROM fact_fts WHERE fact_fts MATCH ?)"
          )
          args.append(_fts_query(q))
      clause = " AND ".join(where)

      conn = open_ro(db_path)
      try:
          total = conn.execute(
              f"SELECT COUNT(*) FROM fact f "
              f"JOIN entity e ON f.subject_id = e.id WHERE {clause}",
              args,
          ).fetchone()[0]
          rows = conn.execute(
              f"SELECT f.*, e.name AS subject FROM fact f "
              f"JOIN entity e ON f.subject_id = e.id WHERE {clause} "
              f"ORDER BY f.created_at DESC, f.id DESC LIMIT ? OFFSET ?",
              (*args, page_size, offset),
          ).fetchall()
      finally:
          conn.close()
      return {
          "items": [dict(r) for r in rows],
          "page": page,
          "page_size": page_size,
          "total": total,
      }


  def get_fact(db_path: Path, fact_id) -> dict | None:
      """Full fact row + subject name + supersession chain (oldest->newest,
      walked both directions) + source episode (spec §7)."""
      conn = open_ro(db_path)
      try:
          row = conn.execute(
              "SELECT f.*, e.name AS subject FROM fact f "
              "JOIN entity e ON f.subject_id = e.id WHERE f.id = ?",
              (fact_id,),
          ).fetchone()
          if row is None:
              return None
          out = dict(row)

          # Walk backward: follow superseded_by chains that point AT this fact
          # (older facts whose superseded_by == current id), oldest first.
          backward: list[dict] = []
          cur = out["id"]
          while True:
              prev = conn.execute(
                  "SELECT * FROM fact WHERE superseded_by = ?", (cur,)
              ).fetchone()
              if prev is None:
                  break
              backward.append(dict(prev))
              cur = prev["id"]
          backward.reverse()  # oldest -> ... -> just-before-current

          # Walk forward: follow this fact's superseded_by pointer to newer facts.
          forward: list[dict] = []
          cur_row = dict(row)
          while cur_row.get("superseded_by"):
              nxt = conn.execute(
                  "SELECT * FROM fact WHERE id = ?", (cur_row["superseded_by"],)
              ).fetchone()
              if nxt is None:
                  break
              forward.append(dict(nxt))
              cur_row = dict(nxt)

          chain = backward + [dict(row)] + forward
          out["chain"] = chain

          episode = conn.execute(
              "SELECT * FROM episode WHERE id = ?", (out["episode_id"],)
          ).fetchone()
          out["episode"] = dict(episode) if episode is not None else None
      finally:
          conn.close()
      return out


  def list_episodes(db_path: Path, page: int = 1, page_size: int = 50) -> dict:
      """Episodes ordered t_ref DESC (spec §7)."""
      page, page_size, offset = _paginate(page, page_size)
      conn = open_ro(db_path)
      try:
          total = conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
          rows = conn.execute(
              "SELECT * FROM episode ORDER BY t_ref DESC, id DESC LIMIT ? OFFSET ?",
              (page_size, offset),
          ).fetchall()
      finally:
          conn.close()
      return {
          "items": [dict(r) for r in rows],
          "page": page,
          "page_size": page_size,
          "total": total,
      }


  def get_episode(db_path: Path, episode_id) -> dict | None:
      """One episode + its extracted facts (episode_id match)."""
      conn = open_ro(db_path)
      try:
          row = conn.execute(
              "SELECT * FROM episode WHERE id = ?", (episode_id,)
          ).fetchone()
          if row is None:
              return None
          out = dict(row)
          facts = conn.execute(
              "SELECT f.*, e.name AS subject FROM fact f "
              "JOIN entity e ON f.subject_id = e.id WHERE f.episode_id = ? "
              "ORDER BY f.created_at, f.id",
              (episode_id,),
          ).fetchall()
          out["facts"] = [dict(r) for r in facts]
      finally:
          conn.close()
      return out


  def list_entities(db_path: Path, page: int = 1, page_size: int = 50) -> dict:
      """Entity names + fact_count (as subject), ordered fact_count DESC then name."""
      page, page_size, offset = _paginate(page, page_size)
      conn = open_ro(db_path)
      try:
          total = conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
          rows = conn.execute(
              "SELECT e.id AS id, e.name AS name, e.type AS type, "
              "COUNT(f.id) AS fact_count "
              "FROM entity e LEFT JOIN fact f ON f.subject_id = e.id "
              "GROUP BY e.id, e.name, e.type "
              "ORDER BY fact_count DESC, e.name LIMIT ? OFFSET ?",
              (page_size, offset),
          ).fetchall()
      finally:
          conn.close()
      return {
          "items": [dict(r) for r in rows],
          "page": page,
          "page_size": page_size,
          "total": total,
      }
  ```
  Also add the FTS helper near the top of the module (below `open_ro`), matching
  the engine's own sanitization so `q` never throws on punctuation:
  ```python
  def _fts_query(text: str) -> str:
      """OR-query of bare alnum terms (mirrors engine sqlite_store._fts_query so
      the console's text filter matches the same tokens the engine indexes)."""
      terms = [
          t for t in "".join(c if c.isalnum() else " " for c in text).split() if t
      ]
      if not terms:
          return '""'
      return " OR ".join(terms)
  ```

- [ ] **Step 4: Run — Expected: PASS.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_inspect_facts.py -v
  ```
  Expected: PASS (latest_only default, predicate, case-insensitive entity,
  min_salience, FTS q, post-filter total, chain oldest->newest, episode carries
  facts, entity fact counts all green).

- [ ] **Step 5: Run the whole console suite to confirm no regressions.**
  ```bash
  console/.venv/bin/python -m pytest console/tests -v
  ```
  Expected: all four test files PASS.

- [ ] **Step 6: Commit.**
  ```bash
  git add console/src/lean_memory_console/inspect_sql.py \
          console/tests/test_inspect_facts.py
  git commit -m "feat(console): inspect_sql facts/chain/episodes/entities readers"
  ```

---

## Contract deviations (and why)

1. **`deploy/docker-compose.yml` is created in Task 1, not merely referenced.**
   The contract says the compose file is force-included into the wheel from
   `../deploy/docker-compose.yml`, but that path does not exist in the repo yet
   (`git log -- deploy/` is empty; no `deploy/` dir). A hatchling `force-include`
   whose source is missing fails the editable install in Task 1 Step 4. Since
   §9 names `deploy/docker-compose.yml` the "single source of truth", Task 1
   creates it (referencing a `deploy/Dockerfile` target that a later task
   supplies — the compose file is inert until then). This is additive and stays
   within lane D (it is under `deploy/`, not `src/lean_memory/`).

2. **`models=="stub"` == default `Memory()` construction (no override wiring in
   these tasks).** The engine's every backend already defaults to its offline
   stub (memory.py:58–64), so forcing stubs needs no explicit override — bare
   `Memory(root=...)` is fully offline. The contract's EngineGateway note about
   "stub backends forced when models=='stub'" is deferred to the engine.py task
   (out of scope for Tasks 1–4); the fixture builder and config here rely only
   on the already-stub defaults. No behavioral deviation, just clarifying that
   Tasks 1–4 add no override plumbing.

3. **`list_namespaces` per-namespace dict uses an `activity` sub-object rather
   than flattened `adds`/`searches`/`earliest_ts` keys.** The contract says
   "activity from event_log.activity_summary"; `activity_summary` already returns
   `{"adds","searches","earliest_ts"}`, so the plan nests that dict under
   `"activity"` verbatim rather than re-inventing key names. If the app layer
   (later task) needs them flattened it can spread `n["activity"]`; the data is
   identical. Flagged so a later task author does not treat the shape as a bug.

4. **`test_config.py` imports `Path` at the bottom of the file.** Cosmetic: the
   `pytest.raises`/`monkeypatch` fixtures read most naturally top-down, and
   `resolve_data_root` tests need `Path`. The import is placed with a `# noqa`
   at file end so the test body reads in execution order; functionally identical
   to a top import. (Optional: a plan executor may hoist it — no test depends on
   its position.)

5. **`get_fact` chain walk covers both a linear forward pointer and a backward
   scan.** The schema has only `superseded_by` (retired -> new); there is no
   explicit "supersedes" back-pointer. The plan walks backward by querying
   `WHERE superseded_by = ?` (facts pointing at the current one) and forward by
   following the current fact's own `superseded_by`. This reconstructs the full
   oldest->newest chain the contract asks for using only the real column, with
   the current fact always present exactly once. No deviation from the requested
   output shape; noting the mechanism since the schema lacks a symmetric column.
# Backend services (Tasks 5–9)

Engine gateway, observing MCP wrapper, human read-path FastAPI app, REST/MCP
data plane, and the CLI. These build on the config/events/inspect_sql modules
authored in Tasks 1–4 and consume the real `lean_memory` engine.

Run all commands from repo root
`/Users/wuesteon/research/lean-memory/.claude/worktrees/memory-ui`.
Console venv is created once (Task 1); if re-creating:

```bash
python3 -m venv console/.venv && console/.venv/bin/pip install -e . -e './console[dev]'
```

Test command form:
`console/.venv/bin/python -m pytest console/tests/<file>::<test> -v`

Source facts baked into this section (verified against the checked-in engine):

- `Memory(root=..., embedder=, reranker=, generator=, router=, typer=,
  contradiction=)` — all keyword-only, all default to offline stubs
  (`FakeEmbedder`, `IdentityReranker`, `StubCandidateGenerator`,
  `RecallBiasedRouter`, `StubTyper`, `ContradictionResolver`). The default
  constructor is already fully offline, so `models=="stub"` and `models=="auto"`
  both build `Memory(root=...)` with no explicit backends; the distinction only
  matters when Task 9 boot-checks real-model importability (out of scope here).
  (`src/lean_memory/memory.py:42-64`)
- `Memory.add(namespace, text, *, t_ref=None, source="user") -> list[str]`
  (fact ids). `t_ref` becomes `Fact.valid_at` (a `TypedFact.valid_at` is passed
  through `_build_fact`; the StubTyper sets `valid_at` from the episode `t_ref`).
  (`src/lean_memory/memory.py:78-134`)
- `Memory.search(namespace, query, k=5, *, as_of=None, is_latest_only=True)
  -> list[RetrievedFact]`. `latest_only` maps to `is_latest_only`.
  (`src/lean_memory/memory.py:173-186`)
- `RetrievedFact` attrs: `.fact` (a `Fact` with `.id`, `.fact_text`),
  `.final_score`, `.relevance`, `.recency`, `.importance`, `.dense_rank`,
  `.sparse_rank`, `.rrf_score`. (`src/lean_memory/types.py:95-107`)
- Store `_connect()` sets `PRAGMA journal_mode=WAL` + `foreign_keys=ON` only —
  no `busy_timeout`. `fact.superseded_by` points retired→new. Table/columns per
  `src/lean_memory/store/schema.py`. (`src/lean_memory/store/sqlite_store.py:50-60`)
- Core MCP server object: `lean_memory.mcp_server.mcp` (a `FastMCP`); tools are
  read via `mcp._tool_manager.list_tools()` → `Tool` objects with `.name`,
  `.parameters` (JSON schema dict), `.fn`. Core exposes `memory_add`,
  `memory_search`, `memory_clear`. (`src/lean_memory/mcp_server.py:50,60-88`)
- Installed `mcp` SDK is 1.28.0. `FastMCP(name)`; `@mcp.tool()` decorator;
  `mcp.run()` runs stdio synchronously; `mcp.streamable_http_app()` returns a
  Starlette app serving at `settings.streamable_http_path` (default `/mcp`);
  `FastMCP(name, stateless_http=True, json_response=True)` makes the HTTP app
  mountable without a long-lived session-manager lifespan. `await
  mcp.call_tool(name, args)` runs a registered tool in-process and returns
  converted content. (`.venv/.../mcp/server/fastmcp/server.py`,
  `.../tools/tool_manager.py`)

---

### Task 5: EngineGateway — the write path and event recording

**Files:**
- Create: `console/src/lean_memory_console/engine.py`
- Modify: `console/pyproject.toml` (add `anyio` dev dep pin for the async test runner)
- Test: `console/tests/test_engine.py`

**Interfaces:**
- Consumes: `lean_memory.Memory`, `config.ConsoleConfig`,
  `config.is_reserved_namespace`, `config.ns_db_path`, `events.EventLog`.
- Produces: `AddResult`, `SearchResult`, `retry_busy`, `EngineGateway`.

- [ ] **Step 1: Pin the async test runner in `console/pyproject.toml`.**
  The gateway is `async`; tests drive it with `anyio` markers. Add `anyio` and
  `pytest` to the `dev` extra (if not already present from Task 1) and register
  the `anyio_backend` fixture in a `conftest.py`. Edit only the `[project.optional-dependencies]` `dev` list and the `[tool.pytest.ini_options]` block —
  add these lines (do not remove existing entries):

  ```toml
  # under [project.optional-dependencies] -> dev = [ ... ] ensure these are present:
  #   "pytest>=8",
  #   "anyio>=4",
  #   "httpx>=0.27",     # used by Tasks 7-8 TestClient
  ```

  Create `console/tests/conftest.py`:

  ```python
  import pytest


  @pytest.fixture
  def anyio_backend():
      return "asyncio"
  ```

- [ ] **Step 2: Write the failing test `console/tests/test_engine.py`.**

  ```python
  import sqlite3

  import pytest

  from lean_memory_console.config import ConsoleConfig
  from lean_memory_console.engine import (
      AddResult,
      EngineGateway,
      SearchResult,
      retry_busy,
  )
  from lean_memory_console.events import EventLog


  def _config(tmp_path):
      return ConsoleConfig(data_root=tmp_path, mode="local", models="stub")


  @pytest.fixture
  def gateway(tmp_path):
      cfg = _config(tmp_path)
      log = EventLog(tmp_path)
      gw = EngineGateway(cfg, log)
      yield gw
      gw.close()
      log.close()


  def test_retry_busy_retries_then_succeeds():
      calls = {"n": 0}

      def flaky():
          calls["n"] += 1
          if calls["n"] < 3:
              raise sqlite3.OperationalError("database is locked")
          return "ok"

      assert retry_busy(flaky, attempts=3) == "ok"
      assert calls["n"] == 3


  def test_retry_busy_reraises_non_lock():
      def boom():
          raise sqlite3.OperationalError("no such table: fact")

      with pytest.raises(sqlite3.OperationalError):
          retry_busy(boom, attempts=3)


  @pytest.mark.anyio
  async def test_add_returns_fact_ids(gateway):
      res = await gateway.add("proj", "Alice works at Acme.")
      assert isinstance(res, AddResult)
      assert res.fact_ids
      assert all(isinstance(fid, str) for fid in res.fact_ids)
      assert res.duration_ms >= 0.0


  @pytest.mark.anyio
  async def test_reserved_namespace_rejected(gateway):
      with pytest.raises(ValueError):
          await gateway.add("_events", "nope")


  @pytest.mark.anyio
  async def test_t_ref_propagates_to_valid_at(gateway, tmp_path):
      t_ref = 1_600_000_000_000
      res = await gateway.add("proj", "Bob likes coffee.", t_ref=t_ref)
      db = tmp_path / "proj.db"
      con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
      con.row_factory = sqlite3.Row
      rows = con.execute(
          "SELECT valid_at FROM fact WHERE id IN (%s)"
          % ",".join("?" * len(res.fact_ids)),
          res.fact_ids,
      ).fetchall()
      con.close()
      assert rows
      assert all(r["valid_at"] == t_ref for r in rows)


  @pytest.mark.anyio
  async def test_contradiction_supersedes_first_add(gateway):
      first = await gateway.add("proj", "The user lives in Paris.")
      assert first.fact_ids
      second = await gateway.add("proj", "The user lives in Berlin.")
      assert second.superseded_count >= 1
      assert set(first.fact_ids) & set(second.superseded_fact_ids)


  @pytest.mark.anyio
  async def test_search_returns_all_nine_hit_keys(gateway):
      await gateway.add("proj", "Carol enjoys hiking in the mountains.")
      res = await gateway.search("proj", "hiking")
      assert isinstance(res, SearchResult)
      assert res.hits
      hit = res.hits[0]
      for key in (
          "fact_id",
          "fact_text",
          "final_score",
          "relevance",
          "recency",
          "importance",
          "dense_rank",
          "sparse_rank",
          "rrf_score",
      ):
          assert key in hit


  @pytest.mark.anyio
  async def test_add_and_search_record_events(gateway, tmp_path):
      await gateway.add("proj", "Dave owns a bike.")
      await gateway.search("proj", "bike", origin="agent")
      log = EventLog(tmp_path)
      adds = log.list_events("proj", kind="add")
      searches = log.list_events("proj", kind="search")
      log.close()
      assert adds["total"] == 1
      assert searches["total"] == 1
      assert searches["items"][0]["payload"]["origin"] == "agent"


  @pytest.mark.anyio
  async def test_search_records_ui_origin(gateway, tmp_path):
      await gateway.add("proj", "Eve plays chess.")
      await gateway.search("proj", "chess", origin="ui")
      log = EventLog(tmp_path)
      searches = log.list_events("proj", kind="search")
      log.close()
      assert searches["items"][0]["payload"]["origin"] == "ui"
  ```

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_engine.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named
  'lean_memory_console.engine'` (module not yet created).

- [ ] **Step 3: Implement `console/src/lean_memory_console/engine.py`.**

  ```python
  """EngineGateway — the console's write path over a single lean_memory.Memory.

  Wraps every engine write in a bounded SQLITE_BUSY retry (the engine sets no
  busy_timeout, §6), serializes per-namespace writes with an asyncio.Lock,
  detects supersession by reading fact.superseded_by on the returned ids while
  still holding the lock (§5), and records add/search events. Event-recording
  never masks the operation's own result (§5 failure contract).
  """

  from __future__ import annotations

  import asyncio
  import sqlite3
  import time
  from dataclasses import dataclass, field
  from typing import Callable, TypeVar

  from lean_memory import Memory

  from .config import ConsoleConfig, is_reserved_namespace, ns_db_path
  from .events import EventLog

  T = TypeVar("T")


  @dataclass
  class AddResult:
      fact_ids: list = field(default_factory=list)
      superseded_fact_ids: list = field(default_factory=list)
      superseded_count: int = 0
      duration_ms: float = 0.0


  @dataclass
  class SearchResult:
      hits: list = field(default_factory=list)
      duration_ms: float = 0.0


  def retry_busy(fn: Callable[[], T], attempts: int = 3) -> T:
      """Call fn, retrying on SQLITE_BUSY / 'database is locked'.

      Catches sqlite3.OperationalError whose message contains 'locked' or
      'busy'; sleeps 0.05 * 2**i between attempts; re-raises after the last.
      Any other error propagates immediately.
      """
      last: Exception | None = None
      for i in range(attempts):
          try:
              return fn()
          except sqlite3.OperationalError as exc:
              msg = str(exc).lower()
              if "locked" not in msg and "busy" not in msg:
                  raise
              last = exc
              if i < attempts - 1:
                  time.sleep(0.05 * (2 ** i))
      assert last is not None
      raise last


  class EngineGateway:
      def __init__(self, config: ConsoleConfig, event_log: EventLog) -> None:
          self._config = config
          self._events = event_log
          # The default Memory constructor is fully offline (stub backends), so
          # both models="stub" and models="auto" build the offline engine here;
          # real-model wiring is a Task 9 boot concern, out of the gateway.
          self._memory = Memory(root=config.data_root)
          self._locks: dict[str, asyncio.Lock] = {}

      def _lock(self, namespace: str) -> asyncio.Lock:
          if namespace not in self._locks:
              self._locks[namespace] = asyncio.Lock()
          return self._locks[namespace]

      def _detect_superseded(self, namespace: str, fact_ids: list) -> list:
          """SELECT id FROM fact WHERE superseded_by IN (<my returned ids>).

          Read-only connection to the namespace DB; scoped to my add's ids so a
          concurrent writer's supersessions cannot leak in (§5). Never raises —
          a read failure degrades supersession reporting to empty, not an error.
          """
          if not fact_ids:
              return []
          path = ns_db_path(self._config.data_root, namespace)
          try:
              con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
          except sqlite3.OperationalError:
              return []
          try:
              con.row_factory = sqlite3.Row
              placeholders = ",".join("?" * len(fact_ids))
              rows = con.execute(
                  f"SELECT id FROM fact WHERE superseded_by IN ({placeholders})",
                  list(fact_ids),
              ).fetchall()
              return [r["id"] for r in rows]
          except sqlite3.Error:
              return []
          finally:
              con.close()

      async def add(
          self,
          namespace: str,
          text: str,
          source: str = "user",
          t_ref: int | None = None,
      ) -> AddResult:
          if is_reserved_namespace(namespace):
              raise ValueError(f"reserved namespace rejected: {namespace!r}")
          start = time.perf_counter()
          async with self._lock(namespace):
              fact_ids = await asyncio.to_thread(
                  lambda: retry_busy(
                      lambda: self._memory.add(
                          namespace, text, t_ref=t_ref, source=source
                      )
                  )
              )
              superseded = self._detect_superseded(namespace, fact_ids)
          duration_ms = (time.perf_counter() - start) * 1000.0
          result = AddResult(
              fact_ids=list(fact_ids),
              superseded_fact_ids=superseded,
              superseded_count=len(superseded),
              duration_ms=duration_ms,
          )
          self._events.record(
              namespace,
              "add",
              duration_ms,
              {
                  "episode_text_chars": len(text),
                  "source": source,
                  "t_ref": t_ref,
                  "fact_ids": result.fact_ids,
                  "fact_count": len(result.fact_ids),
                  "superseded_fact_ids": result.superseded_fact_ids,
                  "superseded_count": result.superseded_count,
              },
          )
          return result

      async def search(
          self,
          namespace: str,
          query: str,
          k: int = 5,
          latest_only: bool = True,
          origin: str = "agent",
      ) -> SearchResult:
          start = time.perf_counter()
          retrieved = await asyncio.to_thread(
              lambda: retry_busy(
                  lambda: self._memory.search(
                      namespace, query, k=k, is_latest_only=latest_only
                  )
              )
          )
          duration_ms = (time.perf_counter() - start) * 1000.0
          hits = [
              {
                  "fact_id": rf.fact.id,
                  "fact_text": rf.fact.fact_text,
                  "final_score": rf.final_score,
                  "relevance": rf.relevance,
                  "recency": rf.recency,
                  "importance": rf.importance,
                  "dense_rank": rf.dense_rank,
                  "sparse_rank": rf.sparse_rank,
                  "rrf_score": rf.rrf_score,
              }
              for rf in retrieved
          ]
          self._events.record(
              namespace,
              "search",
              duration_ms,
              {
                  "query": query,
                  "k": k,
                  "latest_only": latest_only,
                  "origin": origin,
                  "hits": hits,
              },
          )
          return SearchResult(hits=hits, duration_ms=duration_ms)

      def close(self) -> None:
          self._memory.close()
  ```

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_engine.py -v`
  Expected: PASS (10 tests).

- [ ] **Step 4: Commit.**

  ```bash
  git add console/src/lean_memory_console/engine.py console/tests/test_engine.py console/tests/conftest.py console/pyproject.toml
  git commit -m "feat(console): EngineGateway write path, retry_busy, supersession + event recording"
  ```

---

### Task 6: observe_mcp.py — the observing stdio MCP wrapper + parity test

**Files:**
- Create: `console/src/lean_memory_console/observe_mcp.py`
- Test: `console/tests/test_mcp_parity.py`
- Test: `console/tests/test_observe_mcp.py`

**Interfaces:**
- Consumes: `engine.EngineGateway`, `config.load_config`, `mcp.server.fastmcp.FastMCP`.
- Produces: `build_mcp(gateway) -> FastMCP`, `run_stdio(config) -> None`.

- [ ] **Step 1: Write the failing parity test `console/tests/test_mcp_parity.py`.**
  Introspects the core server's registry (`lean_memory.mcp_server.mcp`) and the
  wrapper's, asserting name-set and per-tool arg contracts.

  ```python
  import lean_memory.mcp_server as core_server

  from lean_memory_console.config import ConsoleConfig
  from lean_memory_console.engine import EngineGateway
  from lean_memory_console.events import EventLog
  from lean_memory_console.observe_mcp import build_mcp


  def _tool_params(mcp_obj):
      """{tool_name: set(json-schema property names)} from a FastMCP registry."""
      out = {}
      for tool in mcp_obj._tool_manager.list_tools():
          props = (tool.parameters or {}).get("properties", {})
          out[tool.name] = set(props.keys())
      return out


  def test_wrapper_exposes_exactly_add_and_search(tmp_path):
      cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
      log = EventLog(tmp_path)
      gw = EngineGateway(cfg, log)
      wrapper = build_mcp(gw)
      names = set(_tool_params(wrapper).keys())
      gw.close()
      log.close()
      assert names == {"memory_add", "memory_search"}


  def test_memory_clear_absent(tmp_path):
      cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
      log = EventLog(tmp_path)
      gw = EngineGateway(cfg, log)
      wrapper = build_mcp(gw)
      names = set(_tool_params(wrapper).keys())
      gw.close()
      log.close()
      assert "memory_clear" not in names


  def test_shared_tools_accept_core_args(tmp_path):
      cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
      log = EventLog(tmp_path)
      gw = EngineGateway(cfg, log)
      wrapper = build_mcp(gw)
      wp = _tool_params(wrapper)
      cp = _tool_params(core_server.mcp)
      gw.close()
      log.close()
      # core args are a subset of the wrapper's for each shared tool
      assert cp["memory_add"] <= wp["memory_add"]
      assert cp["memory_search"] <= wp["memory_search"]
      # explicit floor: the Memory-API core args
      assert {"namespace", "text"} <= wp["memory_add"]
      assert {"namespace", "query", "k"} <= wp["memory_search"]


  def test_wrapper_extras_present(tmp_path):
      cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
      log = EventLog(tmp_path)
      gw = EngineGateway(cfg, log)
      wrapper = build_mcp(gw)
      wp = _tool_params(wrapper)
      gw.close()
      log.close()
      # deliberate additions over the core stdio server
      assert {"source", "t_ref"} <= wp["memory_add"]
  ```

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_mcp_parity.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named
  'lean_memory_console.observe_mcp'`.

- [ ] **Step 2: Write the failing functional test `console/tests/test_observe_mcp.py`.**
  Calls the wrapper tools in-process via `await mcp.call_tool(...)`.

  ```python
  import pytest

  from lean_memory_console.config import ConsoleConfig
  from lean_memory_console.engine import EngineGateway
  from lean_memory_console.events import EventLog
  from lean_memory_console.observe_mcp import build_mcp


  @pytest.fixture
  def wrapper(tmp_path):
      cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
      log = EventLog(tmp_path)
      gw = EngineGateway(cfg, log)
      mcp = build_mcp(gw)
      yield mcp, tmp_path
      gw.close()
      log.close()


  def _unwrap(result):
      """FastMCP call_tool returns (content, structured) or structured dict."""
      if isinstance(result, tuple):
          return result[1]
      return result


  @pytest.mark.anyio
  async def test_add_then_search_roundtrip(wrapper):
      mcp, _root = wrapper
      add_out = _unwrap(await mcp.call_tool("memory_add", {
          "namespace": "proj", "text": "Frank drives a red car."
      }))
      assert add_out["fact_ids"]
      assert "superseded_count" in add_out
      search_out = _unwrap(await mcp.call_tool("memory_search", {
          "namespace": "proj", "query": "car", "k": 5
      }))
      assert search_out["hits"]
      assert "fact_text" in search_out["hits"][0]
      assert "final_score" in search_out["hits"][0]


  @pytest.mark.anyio
  async def test_events_written(wrapper):
      mcp, root = wrapper
      await mcp.call_tool("memory_add", {"namespace": "proj", "text": "Gina codes."})
      await mcp.call_tool("memory_search", {"namespace": "proj", "query": "codes"})
      log = EventLog(root)
      adds = log.list_events("proj", kind="add")
      searches = log.list_events("proj", kind="search")
      log.close()
      assert adds["total"] == 1
      assert searches["total"] == 1
      # observing-MCP searches are agent-origin (not "ui")
      assert searches["items"][0]["payload"]["origin"] == "agent"


  @pytest.mark.anyio
  async def test_reserved_namespace_rejected(wrapper):
      mcp, _root = wrapper
      with pytest.raises(Exception) as excinfo:
          await mcp.call_tool("memory_add", {"namespace": "_events", "text": "x"})
      assert "reserved" in str(excinfo.value).lower()
  ```

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_observe_mcp.py -v`
  Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `console/src/lean_memory_console/observe_mcp.py`.**
  Tools are thin async shells over the gateway. `call_tool` runs the async fn
  directly; `mcp.run()` drives stdio. The wrapper deliberately omits
  `memory_clear` (no deletion surface) and adds `source`/`t_ref`.

  ```python
  """Observing MCP wrapper — stdio server that writes through EngineGateway.

  A deliberate superset of the core stdio server: memory_add gains source/t_ref
  and a structured return; memory_clear is intentionally absent (no deletion
  surface, §6). Parity is with the Memory API, not the core tool signatures.
  """

  from __future__ import annotations

  from mcp.server.fastmcp import FastMCP

  from .config import ConsoleConfig, load_config
  from .engine import EngineGateway
  from .events import EventLog


  def build_mcp(gateway: EngineGateway) -> FastMCP:
      mcp = FastMCP("lean-memory-console")

      @mcp.tool()
      async def memory_add(
          namespace: str,
          text: str,
          source: str = "user",
          t_ref: int | None = None,
      ) -> dict:
          """Ingest text into the namespace's memory (observing wrapper).

          Returns the new fact ids and how many prior facts were superseded.
          """
          res = await gateway.add(namespace, text, source=source, t_ref=t_ref)
          return {
              "fact_ids": res.fact_ids,
              "superseded_count": res.superseded_count,
          }

      @mcp.tool()
      async def memory_search(namespace: str, query: str, k: int = 5) -> dict:
          """Search a namespace's memory; returns top-k fact texts + scores.

          Always latest-only (the latest_only flag is REST-only, §6).
          """
          res = await gateway.search(
              namespace, query, k=k, latest_only=True, origin="agent"
          )
          return {
              "hits": [
                  {"fact_text": h["fact_text"], "final_score": h["final_score"]}
                  for h in res.hits
              ]
          }

      return mcp


  def run_stdio(config: ConsoleConfig) -> None:
      """Build the gateway + wrapper for `config` and serve over stdio."""
      event_log = EventLog(config.data_root)
      gateway = EngineGateway(config, event_log)
      mcp = build_mcp(gateway)
      try:
          mcp.run()  # blocks on stdio until the client disconnects
      finally:
          gateway.close()
          event_log.close()
  ```

  Run both:
  `console/.venv/bin/python -m pytest console/tests/test_mcp_parity.py console/tests/test_observe_mcp.py -v`
  Expected: PASS (8 tests). Note on the reserved-namespace test: FastMCP wraps a
  tool's exception in `ToolError` whose message includes the original text, so
  the substring `"reserved"` assertion holds.

- [ ] **Step 4: Commit.**

  ```bash
  git add console/src/lean_memory_console/observe_mcp.py console/tests/test_mcp_parity.py console/tests/test_observe_mcp.py
  git commit -m "feat(console): observing MCP wrapper (memory_add/search) + core-parity test"
  ```

---

### Task 7: app.py + routes/views.py — the human read-path console

**Files:**
- Create: `console/src/lean_memory_console/app.py`
- Create: `console/src/lean_memory_console/routes/__init__.py`
- Create: `console/src/lean_memory_console/routes/views.py`
- Test: `console/tests/test_views.py`

**Interfaces:**
- Consumes: `config.ConsoleConfig`, `engine.EngineGateway`, `events.EventLog`,
  `inspect_sql.*`, `config.sanitize_namespace`, `config.is_reserved_namespace`,
  `config.ns_db_path`.
- Produces: `create_app(config, gateway, event_log) -> FastAPI`, the
  `/views/*` router, `require_auth` dependency, `Referrer-Policy` middleware,
  local-mode Host check.

- [ ] **Step 1: Write the failing test `console/tests/test_views.py`.**
  Drives two app instances (local + docker) against the committed fixture root.
  The fixture builder (`console/tests/fixtures/build_fixture.py`, Task 4)
  produces a data root with 2 namespaces; here we copy it to a tmp dir so the
  writable test-search doesn't mutate the checked-in fixture.

  ```python
  import shutil
  from pathlib import Path

  import pytest
  from fastapi.testclient import TestClient

  from lean_memory_console.app import create_app
  from lean_memory_console.config import ConsoleConfig
  from lean_memory_console.engine import EngineGateway
  from lean_memory_console.events import EventLog

  FIXTURE = Path(__file__).parent / "fixtures" / "data"


  @pytest.fixture
  def data_root(tmp_path):
      root = tmp_path / "data"
      shutil.copytree(FIXTURE, root)
      return root


  def _first_ns(client, token):
      body = client.get("/views/namespaces", params={"token": token}).json()
      return body[0]["name"]


  @pytest.fixture
  def local(data_root):
      cfg = ConsoleConfig(
          data_root=data_root, mode="local", models="stub",
          session_token="sesame",
      )
      log = EventLog(data_root)
      gw = EngineGateway(cfg, log)
      app = create_app(cfg, gw, log)
      client = TestClient(app)
      yield cfg, client
      gw.close()
      log.close()


  @pytest.fixture
  def docker(data_root):
      cfg = ConsoleConfig(
          data_root=data_root, mode="docker", models="stub",
          api_key="secretkey",
      )
      log = EventLog(data_root)
      gw = EngineGateway(cfg, log)
      app = create_app(cfg, gw, log)
      client = TestClient(app)
      yield cfg, client
      gw.close()
      log.close()


  def test_whoami_local_no_auth(local):
      _cfg, client = local
      r = client.get("/views/whoami")
      assert r.status_code == 200
      body = r.json()
      assert body["mode"] == "local"
      assert body["auth"] == "token"
      assert body["authenticated"] is False
      assert "data_root" in body


  def test_whoami_docker_no_auth(docker):
      _cfg, client = docker
      body = client.get("/views/whoami").json()
      assert body["mode"] == "docker"
      assert body["auth"] == "bearer"
      assert body["authenticated"] is False


  def test_whoami_authenticated_local(local):
      _cfg, client = local
      body = client.get("/views/whoami", params={"token": "sesame"}).json()
      assert body["authenticated"] is True


  def test_namespaces_requires_token(local):
      _cfg, client = local
      assert client.get("/views/namespaces").status_code == 401


  def test_namespaces_bad_bearer(docker):
      _cfg, client = docker
      r = client.get(
          "/views/namespaces", headers={"Authorization": "Bearer wrong"}
      )
      assert r.status_code == 401


  def test_token_via_header(local):
      _cfg, client = local
      r = client.get(
          "/views/namespaces", headers={"X-Console-Token": "sesame"}
      )
      assert r.status_code == 200


  def test_referrer_policy_header(local):
      _cfg, client = local
      r = client.get("/views/whoami")
      assert r.headers["Referrer-Policy"] == "no-referrer"


  def test_host_spoof_403_local(local):
      _cfg, client = local
      r = client.get(
          "/views/whoami",
          params={"token": "sesame"},
          headers={"Host": "evil.example.com"},
      )
      assert r.status_code == 403


  def test_facts_pagination_envelope(local):
      _cfg, client = local
      ns = _first_ns(client, "sesame")
      r = client.get(
          f"/views/{ns}/facts",
          params={"token": "sesame", "page": 1, "page_size": 2},
      )
      assert r.status_code == 200
      body = r.json()
      assert set(body.keys()) == {"items", "page", "page_size", "total"}
      assert body["page"] == 1
      assert body["page_size"] == 2
      assert len(body["items"]) <= 2


  def test_fact_detail_has_chain(local):
      _cfg, client = local
      ns = _first_ns(client, "sesame")
      # find a retired fact via latest_only=false so a chain exists
      facts = client.get(
          f"/views/{ns}/facts",
          params={"token": "sesame", "latest_only": "false", "page_size": 200},
      ).json()["items"]
      fid = facts[0]["id"]
      r = client.get(
          f"/views/{ns}/facts/{fid}", params={"token": "sesame"}
      )
      assert r.status_code == 200
      body = r.json()
      assert "chain" in body
      assert isinstance(body["chain"], list)


  def test_events_kind_filter(local):
      _cfg, client = local
      ns = _first_ns(client, "sesame")
      r = client.get(
          f"/views/{ns}/events",
          params={"token": "sesame", "kind": "search"},
      )
      assert r.status_code == 200
      body = r.json()
      assert all(it["kind"] == "search" for it in body["items"])


  def test_test_search_records_ui_and_excluded_from_activity(local):
      _cfg, client = local
      ns = _first_ns(client, "sesame")
      before = client.get(
          "/views/namespaces", params={"token": "sesame"}
      ).json()
      before_searches = next(
          n["activity"]["searches"] for n in before if n["name"] == ns
      )
      r = client.post(
          f"/views/{ns}/test-search",
          params={"token": "sesame"},
          json={"query": "test", "k": 3},
      )
      assert r.status_code == 200
      assert "hits" in r.json()
      # the ui-origin search event was recorded ...
      evs = client.get(
          f"/views/{ns}/events",
          params={"token": "sesame", "kind": "search"},
      ).json()
      assert any(it["payload"].get("origin") == "ui" for it in evs["items"])
      # ... but excluded from the 7-day activity searches count
      after = client.get(
          "/views/namespaces", params={"token": "sesame"}
      ).json()
      after_searches = next(
          n["activity"]["searches"] for n in after if n["name"] == ns
      )
      assert after_searches == before_searches
  ```

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_views.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named
  'lean_memory_console.app'`.

- [ ] **Step 2: Create `console/src/lean_memory_console/routes/__init__.py` (empty package marker).**

  ```python
  ```

- [ ] **Step 3: Implement `console/src/lean_memory_console/app.py`.**

  ```python
  """FastAPI factory for the console (both modes).

  Local mode: 127.0.0.1 bind, per-launch session token (query param or
  X-Console-Token header). Docker mode: Authorization: Bearer <api_key>.
  Referrer-Policy: no-referrer on everything; local mode also validates the
  Host header (DNS-rebinding belt-and-suspenders).
  """

  from __future__ import annotations

  from pathlib import Path

  from fastapi import FastAPI, HTTPException, Request
  from fastapi.responses import JSONResponse
  from fastapi.staticfiles import StaticFiles

  from .config import ConsoleConfig
  from .engine import EngineGateway
  from .events import EventLog
  from .routes.views import build_views_router


  def _is_authenticated(request: Request, config: ConsoleConfig) -> bool:
      if config.mode == "docker":
          header = request.headers.get("Authorization", "")
          expected = f"Bearer {config.api_key}"
          return bool(config.api_key) and header == expected
      # local mode: query token OR X-Console-Token header
      token = request.query_params.get("token") or request.headers.get(
          "X-Console-Token"
      )
      return bool(config.session_token) and token == config.session_token


  def require_auth(request: Request) -> None:
      """FastAPI dependency: 401 unless valid credential is presented."""
      config: ConsoleConfig = request.app.state.config
      if not _is_authenticated(request, config):
          raise HTTPException(status_code=401, detail="unauthorized")


  def create_app(
      config: ConsoleConfig,
      gateway: EngineGateway,
      event_log: EventLog,
  ) -> FastAPI:
      app = FastAPI(title="lean-memory-console")
      app.state.config = config
      app.state.gateway = gateway
      app.state.event_log = event_log

      @app.middleware("http")
      async def _security(request: Request, call_next):
          # Local-mode Host guard (DNS-rebinding belt-and-suspenders).
          if config.mode == "local":
              host = request.headers.get("host", "")
              hostname = host.split(":")[0]
              if not (
                  hostname.startswith("127.0.0.1")
                  or hostname.startswith("localhost")
              ):
                  resp = JSONResponse(
                      {"detail": "forbidden host"}, status_code=403
                  )
                  resp.headers["Referrer-Policy"] = "no-referrer"
                  return resp
          response = await call_next(request)
          response.headers["Referrer-Policy"] = "no-referrer"
          return response

      app.include_router(build_views_router())

      static_dir = Path(__file__).parent / "static"
      if static_dir.is_dir():
          app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")

      return app
  ```

- [ ] **Step 4: Implement `console/src/lean_memory_console/routes/views.py`.**
  Whoami is credential-free for `mode`/`auth`; every other endpoint depends on
  `require_auth`. Enumeration goes through `inspect_sql`; test-search through the
  gateway with `origin="ui"`.

  ```python
  """/views/* — the human read-path router."""

  from __future__ import annotations

  from fastapi import APIRouter, Depends, HTTPException, Request
  from pydantic import BaseModel, Field

  from ..app import require_auth  # noqa: E402  (import after app defines it)
  from ..config import is_reserved_namespace, ns_db_path, sanitize_namespace
  from .. import inspect_sql


  class TestSearchBody(BaseModel):
      query: str
      k: int = Field(default=5, ge=1, le=200)


  def _ns_db(request: Request, namespace: str):
      if is_reserved_namespace(namespace):
          raise HTTPException(status_code=404, detail="unknown namespace")
      config = request.app.state.config
      path = ns_db_path(config.data_root, namespace)
      if not path.exists():
          raise HTTPException(status_code=404, detail="unknown namespace")
      return path


  def build_views_router() -> APIRouter:
      router = APIRouter(prefix="/views")

      @router.get("/whoami")
      def whoami(request: Request):
          config = request.app.state.config
          from ..app import _is_authenticated

          auth = "bearer" if config.mode == "docker" else "token"
          return {
              "mode": config.mode,
              "auth": auth,
              "authenticated": _is_authenticated(request, config),
              "data_root": str(config.data_root),
          }

      @router.get("/namespaces", dependencies=[Depends(require_auth)])
      def namespaces(request: Request):
          config = request.app.state.config
          event_log = request.app.state.event_log
          return inspect_sql.list_namespaces(config.data_root, event_log)

      @router.get("/{namespace}/facts", dependencies=[Depends(require_auth)])
      def facts(
          request: Request,
          namespace: str,
          latest_only: bool = True,
          predicate: str | None = None,
          entity: str | None = None,
          min_salience: float | None = None,
          q: str | None = None,
          page: int = 1,
          page_size: int = 50,
      ):
          path = _ns_db(request, namespace)
          return inspect_sql.list_facts(
              path,
              latest_only=latest_only,
              predicate=predicate,
              entity=entity,
              min_salience=min_salience,
              q=q,
              page=page,
              page_size=page_size,
          )

      @router.get(
          "/{namespace}/facts/{fact_id}", dependencies=[Depends(require_auth)]
      )
      def fact_detail(request: Request, namespace: str, fact_id: str):
          path = _ns_db(request, namespace)
          fact = inspect_sql.get_fact(path, fact_id)
          if fact is None:
              raise HTTPException(status_code=404, detail="unknown fact")
          return fact

      @router.get("/{namespace}/episodes", dependencies=[Depends(require_auth)])
      def episodes(
          request: Request, namespace: str, page: int = 1, page_size: int = 50
      ):
          path = _ns_db(request, namespace)
          return inspect_sql.list_episodes(path, page=page, page_size=page_size)

      @router.get(
          "/{namespace}/episodes/{episode_id}",
          dependencies=[Depends(require_auth)],
      )
      def episode_detail(request: Request, namespace: str, episode_id: str):
          path = _ns_db(request, namespace)
          ep = inspect_sql.get_episode(path, episode_id)
          if ep is None:
              raise HTTPException(status_code=404, detail="unknown episode")
          return ep

      @router.get("/{namespace}/entities", dependencies=[Depends(require_auth)])
      def entities(
          request: Request, namespace: str, page: int = 1, page_size: int = 50
      ):
          path = _ns_db(request, namespace)
          return inspect_sql.list_entities(path, page=page, page_size=page_size)

      @router.get("/{namespace}/events", dependencies=[Depends(require_auth)])
      def events(
          request: Request,
          namespace: str,
          kind: str | None = None,
          page: int = 1,
          page_size: int = 50,
      ):
          if kind is not None and kind not in ("add", "search"):
              raise HTTPException(status_code=422, detail="invalid kind")
          # events live in the sidecar, not a namespace .db — no _ns_db guard,
          # but reserved namespaces are still rejected.
          if is_reserved_namespace(namespace):
              raise HTTPException(status_code=404, detail="unknown namespace")
          event_log = request.app.state.event_log
          return event_log.list_events(
              namespace, kind=kind, page=page, page_size=page_size
          )

      @router.post(
          "/{namespace}/test-search", dependencies=[Depends(require_auth)]
      )
      async def test_search(
          request: Request, namespace: str, body: TestSearchBody
      ):
          if is_reserved_namespace(namespace):
              raise HTTPException(status_code=404, detail="unknown namespace")
          gateway = request.app.state.gateway
          result = await gateway.search(
              namespace, body.query, k=body.k, latest_only=True, origin="ui"
          )
          return {"hits": result.hits, "duration_ms": result.duration_ms}

      return router
  ```

  Note the import-cycle handling: `routes/views.py` imports `require_auth`/
  `_is_authenticated` from `..app`, and `app.py` imports `build_views_router`
  from `.routes.views`. Python resolves this because `create_app` (the only
  place that calls `build_views_router`) runs after both modules are fully
  imported; the top-level `from ..app import require_auth` succeeds because
  `app.py` defines `require_auth` before it imports `views` (the
  `from .routes.views import build_views_router` line is the last import in
  `app.py`). If a circular-import error surfaces at collection time, move the
  `from ..app import ...` lines inside `build_views_router`.

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_views.py -v`
  Expected: PASS (13 tests).

- [ ] **Step 5: Commit.**

  ```bash
  git add console/src/lean_memory_console/app.py console/src/lean_memory_console/routes/__init__.py console/src/lean_memory_console/routes/views.py console/tests/test_views.py
  git commit -m "feat(console): FastAPI app factory + /views read-path router (auth, host guard, test-search)"
  ```

---

### Task 8: routes/data.py REST mirror + routes/mcp.py Docker MCP mount

**Files:**
- Create: `console/src/lean_memory_console/routes/data.py`
- Create: `console/src/lean_memory_console/routes/mcp.py`
- Modify: `console/src/lean_memory_console/app.py` (mount data + docker MCP)
- Test: `console/tests/test_data_plane.py`

**Interfaces:**
- Consumes: `engine.EngineGateway`, `observe_mcp.build_mcp`,
  `config.is_reserved_namespace`, `mcp.streamable_http_app()`.
- Produces: `build_data_router()`, `build_mcp_mount(gateway, config) -> ASGI app`,
  wired into `create_app`.

- [ ] **Step 1: Write the failing test `console/tests/test_data_plane.py`.**

  ```python
  import sqlite3

  import pytest
  from fastapi.testclient import TestClient

  from lean_memory_console.app import create_app
  from lean_memory_console.config import ConsoleConfig
  from lean_memory_console.engine import EngineGateway
  from lean_memory_console.events import EventLog


  @pytest.fixture
  def docker(tmp_path):
      root = tmp_path / "data"
      root.mkdir()
      cfg = ConsoleConfig(
          data_root=root, mode="docker", models="stub", api_key="k",
      )
      log = EventLog(root)
      gw = EngineGateway(cfg, log)
      app = create_app(cfg, gw, log)
      client = TestClient(app)
      yield cfg, client, root
      gw.close()
      log.close()


  AUTH = {"Authorization": "Bearer k"}


  def test_add_search_roundtrip(docker):
      _cfg, client, _root = docker
      r = client.post(
          "/v1/proj/memories",
          headers=AUTH,
          json={"text": "Helen sails boats."},
      )
      assert r.status_code == 200
      body = r.json()
      assert body["fact_ids"]
      assert "superseded_count" in body

      s = client.post(
          "/v1/proj/search", headers=AUTH, json={"query": "boats", "k": 5}
      )
      assert s.status_code == 200
      hits = s.json()["hits"]
      assert hits
      # §5 payload keys present on REST hit objects
      for key in (
          "fact_id", "fact_text", "final_score", "relevance", "recency",
          "importance", "dense_rank", "sparse_rank", "rrf_score",
      ):
          assert key in hits[0]


  def test_t_ref_to_valid_at(docker):
      _cfg, client, root = docker
      t_ref = 1_600_000_000_000
      r = client.post(
          "/v1/proj/memories",
          headers=AUTH,
          json={"text": "Ivan runs daily.", "t_ref": t_ref},
      )
      ids = r.json()["fact_ids"]
      con = sqlite3.connect(f"file:{root / 'proj.db'}?mode=ro", uri=True)
      con.row_factory = sqlite3.Row
      rows = con.execute(
          "SELECT valid_at FROM fact WHERE id IN (%s)" % ",".join("?" * len(ids)),
          ids,
      ).fetchall()
      con.close()
      assert all(row["valid_at"] == t_ref for row in rows)


  def test_latest_only_flag_honored(docker):
      _cfg, client, _root = docker
      client.post(
          "/v1/proj/memories", headers=AUTH,
          json={"text": "The user lives in Rome."},
      )
      client.post(
          "/v1/proj/memories", headers=AUTH,
          json={"text": "The user lives in Oslo."},
      )
      latest = client.post(
          "/v1/proj/search", headers=AUTH,
          json={"query": "lives", "latest_only": True},
      ).json()["hits"]
      allhits = client.post(
          "/v1/proj/search", headers=AUTH,
          json={"query": "lives", "latest_only": False},
      ).json()["hits"]
      assert len(allhits) >= len(latest)


  def test_reserved_ns_404(docker):
      _cfg, client, _root = docker
      r = client.post(
          "/v1/_events/memories", headers=AUTH, json={"text": "x"}
      )
      assert r.status_code == 404


  def test_bearer_required(docker):
      _cfg, client, _root = docker
      r = client.post("/v1/proj/memories", json={"text": "x"})
      assert r.status_code == 401


  def test_validation_422(docker):
      _cfg, client, _root = docker
      r = client.post("/v1/proj/memories", headers=AUTH, json={})
      assert r.status_code == 422


  def test_mcp_mount_unauthorized_401(docker):
      _cfg, client, _root = docker
      # No Authorization header -> the ASGI bearer wrapper rejects before MCP.
      r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
      assert r.status_code == 401


  def test_mcp_mount_exists(docker):
      _cfg, client, _root = docker
      # With a valid bearer the wrapper delegates to the MCP app (which then
      # applies its own protocol handling). We assert only that the bearer
      # wrapper does NOT 401 — full MCP-over-HTTP round-trip is deferred to the
      # manual E2E (see the note at the end of this task).
      r = client.post(
          "/mcp",
          headers={"Authorization": "Bearer k", "Accept": "application/json, text/event-stream"},
          json={"jsonrpc": "2.0", "method": "ping", "id": 1},
      )
      assert r.status_code != 401
  ```

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_data_plane.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named
  'lean_memory_console.routes.data'` (import inside app fails / route absent).

- [ ] **Step 2: Implement `console/src/lean_memory_console/routes/data.py`.**

  ```python
  """/v1/* — the REST data plane mirror (Docker mode, non-MCP agents)."""

  from __future__ import annotations

  from fastapi import APIRouter, HTTPException, Request
  from pydantic import BaseModel, Field

  from ..config import is_reserved_namespace


  class MemoryBody(BaseModel):
      text: str
      source: str = "user"
      t_ref: int | None = None


  class SearchBody(BaseModel):
      query: str
      k: int = Field(default=5, ge=1, le=200)
      latest_only: bool = True


  def build_data_router() -> APIRouter:
      router = APIRouter(prefix="/v1")

      @router.post("/{namespace}/memories")
      async def add_memory(request: Request, namespace: str, body: MemoryBody):
          if is_reserved_namespace(namespace):
              raise HTTPException(status_code=404, detail="unknown namespace")
          gateway = request.app.state.gateway
          res = await gateway.add(
              namespace, body.text, source=body.source, t_ref=body.t_ref
          )
          return {
              "fact_ids": res.fact_ids,
              "superseded_count": res.superseded_count,
          }

      @router.post("/{namespace}/search")
      async def search_memory(request: Request, namespace: str, body: SearchBody):
          if is_reserved_namespace(namespace):
              raise HTTPException(status_code=404, detail="unknown namespace")
          gateway = request.app.state.gateway
          res = await gateway.search(
              namespace,
              body.query,
              k=body.k,
              latest_only=body.latest_only,
              origin="agent",
          )
          return {"hits": res.hits, "duration_ms": res.duration_ms}

      return router
  ```

- [ ] **Step 3: Implement `console/src/lean_memory_console/routes/mcp.py`.**
  Builds the observing wrapper's streamable-HTTP app (stateless so it mounts
  without a persistent lifespan) and wraps it with a bearer-check ASGI app.

  ```python
  """Docker-mode streamable-HTTP MCP mount (/mcp) with bearer auth.

  The wrapper FastMCP is built stateless (json_response) so its Starlette app
  mounts into FastAPI without a long-lived session-manager lifespan. A thin ASGI
  wrapper enforces Authorization: Bearer <api_key> before delegating; full
  MCP-over-HTTP round-trips are exercised in the manual E2E, not here.
  """

  from __future__ import annotations

  from mcp.server.fastmcp import FastMCP

  from ..config import ConsoleConfig
  from ..engine import EngineGateway


  def _build_http_mcp(gateway: EngineGateway) -> FastMCP:
      mcp = FastMCP(
          "lean-memory-console",
          stateless_http=True,
          json_response=True,
      )

      @mcp.tool()
      async def memory_add(
          namespace: str,
          text: str,
          source: str = "user",
          t_ref: int | None = None,
      ) -> dict:
          """Ingest text into the namespace's memory (HTTP wrapper)."""
          res = await gateway.add(namespace, text, source=source, t_ref=t_ref)
          return {
              "fact_ids": res.fact_ids,
              "superseded_count": res.superseded_count,
          }

      @mcp.tool()
      async def memory_search(namespace: str, query: str, k: int = 5) -> dict:
          """Search a namespace's memory (HTTP wrapper); always latest-only."""
          res = await gateway.search(
              namespace, query, k=k, latest_only=True, origin="agent"
          )
          return {
              "hits": [
                  {"fact_text": h["fact_text"], "final_score": h["final_score"]}
                  for h in res.hits
              ]
          }

      return mcp


  def build_mcp_mount(gateway: EngineGateway, config: ConsoleConfig):
      """Return an ASGI app: bearer gate → streamable-HTTP MCP app.

      Mount at "/mcp"; the inner MCP app serves at its own root "/" once mounted
      (FastMCP's streamable_http_path is normalized under the mount point).
      """
      mcp = _build_http_mcp(gateway)
      inner = mcp.streamable_http_app()
      expected = f"Bearer {config.api_key}"

      async def gated(scope, receive, send):
          if scope["type"] != "http":
              await inner(scope, receive, send)
              return
          headers = dict(scope.get("headers") or [])
          auth = headers.get(b"authorization", b"").decode()
          if not config.api_key or auth != expected:
              await _send_401(send)
              return
          await inner(scope, receive, send)

      return gated


  async def _send_401(send):
      await send(
          {
              "type": "http.response.start",
              "status": 401,
              "headers": [
                  (b"content-type", b"application/json"),
                  (b"referrer-policy", b"no-referrer"),
              ],
          }
      )
      await send(
          {
              "type": "http.response.body",
              "body": b'{"detail":"unauthorized"}',
          }
      )
  ```

  Note on the mount path: FastMCP's `streamable_http_app()` registers its route
  at `settings.streamable_http_path` (default `/mcp`). When we mount that app at
  `/mcp` in FastAPI, the inner route would resolve at `/mcp/mcp`. To serve at
  exactly `/mcp`, build the FastMCP with `streamable_http_path="/"`:

  ```python
  # in _build_http_mcp, adjust the constructor:
      mcp = FastMCP(
          "lean-memory-console",
          stateless_http=True,
          json_response=True,
          streamable_http_path="/",
      )
  ```

  (The `streamable_http_path` setting is a documented FastMCP Settings field,
  verified in the installed SDK at `mcp/server/fastmcp/server.py`.)

- [ ] **Step 4: Wire the routers/mount into `app.py`.**
  Add the data router (both modes; auth enforced by mode) and, in Docker mode
  only, mount the bearer-gated MCP app. Edit `create_app` in
  `console/src/lean_memory_console/app.py`.

  Add these imports near the top of `app.py`:

  ```python
  from .routes.data import build_data_router
  from .routes.mcp import build_mcp_mount
  ```

  The `/v1/*` routes need auth too. Because they are POST handlers that already
  read `request.app.state.gateway`, gate them with the same dependency by
  including the router with a router-level dependency. Replace the
  `app.include_router(build_views_router())` block with:

  ```python
      from .app import require_auth  # self-reference resolved at call time
      app.include_router(build_views_router())
      app.include_router(
          build_data_router(), dependencies=[Depends(require_auth)]
      )

      if config.mode == "docker":
          app.mount("/mcp", build_mcp_mount(gateway, config))
  ```

  and add `Depends` to the FastAPI import line:

  ```python
  from fastapi import Depends, FastAPI, HTTPException, Request
  ```

  Because `require_auth` is defined in this same module, use it directly (drop
  the `from .app import require_auth` self-import — reference the local name):

  ```python
      app.include_router(build_views_router())
      app.include_router(
          build_data_router(), dependencies=[Depends(require_auth)]
      )
      if config.mode == "docker":
          app.mount("/mcp", build_mcp_mount(gateway, config))
  ```

  The static SPA mount at `/` must remain the LAST mount so it does not shadow
  `/v1`, `/views`, or `/mcp` (Starlette matches routes before the catch-all
  StaticFiles mount, but keeping the SPA last is the safe ordering).

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_data_plane.py -v`
  Expected: PASS (8 tests).

  **Deferred to manual E2E (stated explicitly per the task):** a full
  MCP-over-HTTP client round-trip (initialize → tools/list → tools/call over
  streamable-HTTP) against the `/mcp` mount is NOT covered by an automated test.
  `test_mcp_mount_exists` asserts only that a valid bearer is not rejected by the
  ASGI gate; the protocol-level handshake is verified in the pre-merge manual
  E2E (§12: `docker compose up`, `claude mcp add --transport http …`, live
  add/search).

- [ ] **Step 5: Commit.**

  ```bash
  git add console/src/lean_memory_console/routes/data.py console/src/lean_memory_console/routes/mcp.py console/src/lean_memory_console/app.py console/tests/test_data_plane.py
  git commit -m "feat(console): REST /v1 data plane + Docker streamable-HTTP MCP mount with bearer gate"
  ```

---

### Task 9: cli.py — entry point, boot validation, --print-compose-path

**Files:**
- Create: `console/src/lean_memory_console/cli.py`
- Create: `console/src/lean_memory_console/deploy/__init__.py` (package marker so
  `importlib.resources` can address the packaged compose file)
- Modify: `console/pyproject.toml` (console script entry + hatch force-include of
  `deploy/docker-compose.yml` into `lean_memory_console/deploy/`)
- Test: `console/tests/test_cli.py`

**Interfaces:**
- Consumes: `config.load_config`, `config.resolve_data_root`,
  `observe_mcp.run_stdio`, `importlib.resources`.
- Produces: `main(argv=None) -> int`.

Note: `deploy/docker-compose.yml` already exists at the repo root with its
final content (created in Task 1 Step 1; Task 15 adds the Dockerfile and pins
the compose invariants with static tests). This task only packages it into the
wheel and exposes its path; the CLI logic and both resolution branches are what
it verifies.

- [ ] **Step 1: Write the failing test `console/tests/test_cli.py`.**

  ```python
  import os
  from pathlib import Path

  import pytest

  from lean_memory_console import cli


  def test_print_compose_path_exists(capsys):
      rc = cli.main(["--print-compose-path"])
      assert rc == 0
      out = capsys.readouterr().out.strip()
      assert out
      assert Path(out).exists()


  def test_serve_boot_fails_on_unreadable_root(tmp_path, monkeypatch):
      bad = tmp_path / "nope"  # does not exist and cannot be read
      with pytest.raises(SystemExit) as ei:
          cli.main(["serve", "--root", str(bad)])
      assert ei.value.code == 2


  def test_docker_mode_requires_api_key(tmp_path, monkeypatch):
      monkeypatch.delenv("LM_API_KEY", raising=False)
      monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
      # 'mcp' subcommand in a docker context: load_config('docker') must exit 2.
      # We drive load_config directly through a docker boot to assert exit 2.
      from lean_memory_console.config import load_config

      with pytest.raises(SystemExit) as ei:
          load_config("docker", cli_root=str(tmp_path))
      assert ei.value.code == 2


  def test_mcp_subcommand_wires_run_stdio(tmp_path, monkeypatch):
      called = {}

      def fake_run_stdio(config):
          called["root"] = config.data_root

      monkeypatch.setattr(cli, "run_stdio", fake_run_stdio)
      rc = cli.main(["mcp", "--root", str(tmp_path)])
      assert rc == 0
      assert called["root"] == tmp_path


  def test_serve_boot_ok_readable_root(tmp_path, monkeypatch):
      # serve requires only readability; a fresh writable dir passes and the
      # server start is monkeypatched so we don't block.
      started = {}

      def fake_serve(config, no_open):
          started["port"] = config.port

      monkeypatch.setattr(cli, "_run_server", fake_serve)
      rc = cli.main(["serve", "--root", str(tmp_path), "--no-open", "--port", "9999"])
      assert rc == 0
      assert started["port"] == 9999
  ```

  Run:
  `console/.venv/bin/python -m pytest console/tests/test_cli.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named
  'lean_memory_console.cli'`.

- [ ] **Step 2: Create the packaged-deploy marker and a placeholder compose file.**
  Create `console/src/lean_memory_console/deploy/__init__.py`:

  ```python
  ```

  `deploy/docker-compose.yml` already exists at the repo root with its final
  content (Task 1 Step 1). Verify it is present before wiring the resolver:

  ```bash
  test -f deploy/docker-compose.yml && echo OK
  ```
  Expected: `OK`.

- [ ] **Step 3: Implement `console/src/lean_memory_console/cli.py`.**
  `--print-compose-path` resolves the packaged copy via `importlib.resources`,
  with a dev fallback to the repo `deploy/` when the packaged resource is absent
  (editable installs may not map the hatch force-include). Boot validation per
  §10: serve needs a readable root; docker needs `LM_API_KEY` (enforced by
  `load_config`); all fail fast with exit code 2.

  ```python
  """`lean-memory-console` CLI: serve | mcp, plus --print-compose-path."""

  from __future__ import annotations

  import argparse
  import importlib.resources
  import os
  import sys
  from pathlib import Path

  from .config import ConsoleConfig, load_config, resolve_data_root
  from .observe_mcp import run_stdio


  def _compose_path() -> Path:
      """Path to the packaged docker-compose.yml.

      Primary: importlib.resources over the installed package's deploy/ dir
      (the wheel force-includes deploy/docker-compose.yml there). Dev fallback:
      resolve the repo's deploy/docker-compose.yml relative to this file when
      the packaged resource is missing (editable installs may not map the
      force-include).
      """
      try:
          res = importlib.resources.files("lean_memory_console").joinpath(
              "deploy/docker-compose.yml"
          )
          if res.is_file():
              return Path(str(res))
      except (ModuleNotFoundError, FileNotFoundError):
          pass
      # Dev fallback: repo_root/deploy/docker-compose.yml.
      # cli.py -> lean_memory_console -> src -> console -> repo_root
      repo_root = Path(__file__).resolve().parents[4]
      return repo_root / "deploy" / "docker-compose.yml"


  def _validate_serve_root(root: Path) -> None:
      if not root.exists() or not os.access(root, os.R_OK):
          sys.stderr.write(
              f"error: data root not readable: {root}\n"
          )
          raise SystemExit(2)


  def _run_server(config: ConsoleConfig, no_open: bool) -> None:  # pragma: no cover
      """Start uvicorn on 127.0.0.1 (real entry; monkeypatched in tests)."""
      import uvicorn

      from .app import create_app
      from .engine import EngineGateway
      from .events import EventLog

      event_log = EventLog(config.data_root)
      gateway = EngineGateway(config, event_log)
      app = create_app(config, gateway, event_log)
      url = f"http://127.0.0.1:{config.port}/?token={config.session_token}"
      if not no_open:
          import webbrowser

          webbrowser.open(url)
      sys.stdout.write(f"lean-memory-console serving at {url}\n")
      try:
          uvicorn.run(app, host="127.0.0.1", port=config.port, log_level="info")
      finally:
          gateway.close()
          event_log.close()


  def main(argv=None) -> int:
      parser = argparse.ArgumentParser(prog="lean-memory-console")
      parser.add_argument(
          "--print-compose-path", action="store_true",
          help="print the packaged docker-compose.yml path and exit",
      )
      sub = parser.add_subparsers(dest="command")

      p_serve = sub.add_parser("serve", help="run the local read-only console")
      p_serve.add_argument("--root", default=None)
      p_serve.add_argument("--port", type=int, default=None)
      p_serve.add_argument("--no-open", action="store_true")

      p_mcp = sub.add_parser("mcp", help="run the observing MCP stdio server")
      p_mcp.add_argument("--root", default=None)

      args = parser.parse_args(argv)

      if args.print_compose_path:
          sys.stdout.write(f"{_compose_path()}\n")
          return 0

      if args.command == "serve":
          root = resolve_data_root(args.root)
          _validate_serve_root(root)
          config = load_config("local", cli_root=args.root, port=args.port)
          _run_server(config, no_open=args.no_open)
          return 0

      if args.command == "mcp":
          config = load_config("local", cli_root=args.root)
          run_stdio(config)
          return 0

      parser.print_help()
      return 1
  ```

  Note on `_validate_serve_root` + `resolve_data_root`: `resolve_data_root`
  (Task 1) applies `--root > LM_DATA_ROOT > ~/.lean_memory` and does NOT
  auto-create for serve, so an unreadable/nonexistent root reaches the validator
  and triggers `SystemExit(2)`. `load_config("local", ...)` mints the
  `session_token`; `load_config("docker", ...)` is where `LM_API_KEY`-missing
  raises `SystemExit(2)` (Task 1 contract), which the docker-mode test drives
  directly.

- [ ] **Step 4: Add the console script + force-include to `console/pyproject.toml`.**
  Add the entry point and hatch build config (edit only console's pyproject,
  never the root one — lane D):

  ```toml
  [project.scripts]
  lean-memory-console = "lean_memory_console.cli:main"

  [tool.hatch.build.targets.wheel.force-include]
  "../deploy/docker-compose.yml" = "lean_memory_console/deploy/docker-compose.yml"
  ```

  (If `[project.scripts]` or the hatch build table already exist from Task 1,
  merge these keys in rather than duplicating the tables.)

- [ ] **Step 5: Run the CLI tests.**
  Run:
  `console/.venv/bin/python -m pytest console/tests/test_cli.py -v`
  Expected: PASS (5 tests). `test_print_compose_path_exists` passes via the dev
  fallback in an editable install; after a real wheel build it passes via the
  packaged resource — both branches are covered (fallback here; the packaged
  branch is exercised in the deployment task's wheel-build check).

- [ ] **Step 6: Commit.**

  ```bash
  git add console/src/lean_memory_console/cli.py console/src/lean_memory_console/deploy/__init__.py console/pyproject.toml console/tests/test_cli.py deploy/docker-compose.yml
  git commit -m "feat(console): CLI serve|mcp entry point, boot validation, --print-compose-path"
  ```

- [ ] **Step 7: Full-suite green check.**
  Run the whole console suite to confirm Tasks 5–9 integrate:
  `console/.venv/bin/python -m pytest console/tests/ -v`
  Expected: PASS (all tests from Tasks 1–9 green; offline, no network).
# Frontend SPA (Tasks 10-14)

The console UI is a React 18 + TypeScript single-page app built with Bun +
Vite, using react-router for page routing, Tailwind CSS v4 (via the
`@tailwindcss/vite` plugin) for styling, and Recharts only for the Overview
sparklines. The build emits into `console/src/lean_memory_console/static/`
(gitignored) so the FastAPI app of Task 9 can mount it.

> **Design note.** Visual polish (typography scale, color system, spacing
> rhythm, the supersession "wedge" timeline visual) is applied at *execution*
> time via the `frontend-design` skill — that skill is invoked when a human
> runs these tasks, not encoded here. The `.tsx` code written in the steps
> below must already be **functional and reasonably styled with Tailwind
> utility classes**: real layouts, readable defaults, working states. Polish
> re-skins; it does not add missing behavior. Do not defer functionality to
> "design time."

**Contract anchors (baked from source reads).** The read endpoints and their
shapes are the §7 contract produced by Tasks 5-9; the frontend only *consumes*
them. Concretely, verified against
`src/lean_memory/store/schema.py` and `src/lean_memory/types.py`:

- Fact columns surfaced by `/views/{ns}/facts` rows: `fact_id`, `fact_text`,
  `subject` (= `entity.name` via `subject_id`), `predicate`, `object_literal`,
  `salience` (0-10 float), `confidence`, `is_latest` (0/1), `access_count`,
  `valid_at`, `valid_to`, `superseded_by`, `episode_id`, `created_at`.
- Search hit fields (from `RetrievedFact`): `fact_id` (`.fact.id`),
  `fact_text` (`.fact.fact_text`), and the seven top-level scores
  `final_score`, `relevance`, `recency`, `importance`, `dense_rank`,
  `sparse_rank`, `rrf_score`.
- Namespace card fields (from `inspect_sql.list_namespaces`): `name`,
  `facts_latest`, `facts_retired`, `entities`, `episodes`, `chains`,
  `file_size`, `top_predicates` (list of `{predicate, count}`), and an
  `activity` object `{adds, searches, earliest_ts}`.
- Envelope for all list endpoints: `{items, page, page_size, total}`, `page`
  1-based, `page_size` default 50 cap 200, `total` post-filter.
- `whoami`: `{mode: "local"|"docker", auth: "token"|"bearer",
  authenticated: bool, data_root: str}`.

All frontend commands run with `bun` from `ui/`. All Tasks 10-14 end with the
same gate — `bun run typecheck && bun run build` — and a commit that touches
**only** `ui/` and `console/.gitignore` (never `src/lean_memory/`, `bench/`,
`tests/`, or the root `pyproject.toml`, per the lane-D rule).

---

### Task 10: `ui/` scaffold, typed API client, router shell + auth

Stand up the Vite + Bun + React + Tailwind v4 project, wire the dev proxy to
the running console, and implement the whole auth story in a single typed
`api.ts`: `whoami()` first, then mode handling — local reads `?token` from the
URL and strips it via `history.replaceState`, keeping it in module memory and
sending it as `X-Console-Token`; docker prompts for the key and sends
`Authorization: Bearer`. The `App.tsx` router shell mounts the pages (stubbed
here, filled in Tasks 11-14) behind a namespace switcher fed by
`/views/namespaces`, and renders login/error screens per the whoami logic.

**Files:**
- Create: `ui/package.json`
- Create: `ui/tsconfig.json`
- Create: `ui/tsconfig.node.json`
- Create: `ui/vite.config.ts`
- Create: `ui/index.html`
- Create: `ui/.gitignore`
- Create: `ui/src/main.tsx`
- Create: `ui/src/index.css`
- Create: `ui/src/vite-env.d.ts`
- Create: `ui/src/api.ts`
- Create: `ui/src/types.ts`
- Create: `ui/src/auth.tsx`
- Create: `ui/src/App.tsx`
- Create: `ui/src/components/Layout.tsx`
- Create: `ui/src/pages/Overview.tsx` (stub, real in Task 11)
- Create: `ui/src/pages/Memories.tsx` (stub, real in Task 12)
- Create: `ui/src/pages/Episodes.tsx` (stub, real in Task 13)
- Create: `ui/src/pages/Activity.tsx` (stub, real in Task 14)
- Modify: `console/.gitignore` (add `src/lean_memory_console/static/`)
- Test (gate, not pytest): `bun run typecheck && bun run build` from `ui/`

**Interfaces:**
- Consumes: `GET /views/whoami`, `GET /views/namespaces` (§7).
- Produces: `ui/src/api.ts` typed client (`whoami`, `listNamespaces`,
  `listFacts`, `getFact`, `listEpisodes`, `getEpisode`, `listEntities`,
  `listEvents`, `testSearch`), the `AuthProvider`/`useAuth`
  context, `Envelope<T>` type, and the built SPA at
  `console/src/lean_memory_console/static/index.html` consumed by Task 9's
  static mount.

- [ ] **Step 1: Add the static-build path to `console/.gitignore`.**
  Append the built-SPA directory so the Vite output never gets committed.
  Create or extend `console/.gitignore`:

  ```gitignore
  # Built SPA (produced by ui/ via `bun run build`; never committed)
  src/lean_memory_console/static/
  ```

  Commit gate note: this is the ONLY console-side file any frontend task
  touches.

- [ ] **Step 2: Write `ui/package.json` with pinned deps.**
  Pin exact versions (no `^`) so the build is reproducible. Tailwind v4 ships
  as the `tailwindcss` package plus the `@tailwindcss/vite` plugin; no
  `tailwind.config.js` and no PostCSS are required in v4.

  ```json
  {
    "name": "lean-memory-console-ui",
    "private": true,
    "version": "0.1.0",
    "type": "module",
    "scripts": {
      "dev": "vite",
      "build": "vite build",
      "typecheck": "tsc --noEmit",
      "preview": "vite preview"
    },
    "dependencies": {
      "react": "18.3.1",
      "react-dom": "18.3.1",
      "react-router-dom": "6.26.2",
      "recharts": "2.12.7"
    },
    "devDependencies": {
      "@tailwindcss/vite": "4.0.0",
      "@types/react": "18.3.11",
      "@types/react-dom": "18.3.0",
      "@vitejs/plugin-react": "4.3.2",
      "tailwindcss": "4.0.0",
      "typescript": "5.6.2",
      "vite": "5.4.8"
    }
  }
  ```

- [ ] **Step 3: Write the TypeScript configs.**
  `ui/tsconfig.json`:

  ```json
  {
    "compilerOptions": {
      "target": "ES2020",
      "useDefineForClassFields": true,
      "lib": ["ES2020", "DOM", "DOM.Iterable"],
      "module": "ESNext",
      "skipLibCheck": true,
      "moduleResolution": "bundler",
      "allowImportingTsExtensions": true,
      "resolveJsonModule": true,
      "isolatedModules": true,
      "noEmit": true,
      "jsx": "react-jsx",
      "strict": true,
      "noUnusedLocals": true,
      "noUnusedParameters": true,
      "noFallthroughCasesInSwitch": true
    },
    "include": ["src"],
    "references": [{ "path": "./tsconfig.node.json" }]
  }
  ```

  `ui/tsconfig.node.json`:

  ```json
  {
    "compilerOptions": {
      "composite": true,
      "skipLibCheck": true,
      "module": "ESNext",
      "moduleResolution": "bundler",
      "allowSyntheticDefaultImports": true,
      "strict": true
    },
    "include": ["vite.config.ts"]
  }
  ```

- [ ] **Step 4: Write `ui/vite.config.ts`.**
  Output into the console package's `static/` dir with `emptyOutDir` so stale
  assets are cleared; register the React and Tailwind v4 plugins; proxy
  `/views` and `/v1` to the running console on `127.0.0.1:8377` so `bun run
  dev` talks to a live backend.

  ```ts
  import { defineConfig } from "vite";
  import react from "@vitejs/plugin-react";
  import tailwindcss from "@tailwindcss/vite";

  export default defineConfig({
    plugins: [react(), tailwindcss()],
    build: {
      outDir: "../console/src/lean_memory_console/static",
      emptyOutDir: true,
    },
    server: {
      proxy: {
        "/views": "http://127.0.0.1:8377",
        "/v1": "http://127.0.0.1:8377",
      },
    },
  });
  ```

- [ ] **Step 5: Write `ui/index.html`, `ui/.gitignore`, and `ui/src/main.tsx`.**
  `ui/index.html`:

  ```html
  <!doctype html>
  <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <meta name="referrer" content="no-referrer" />
      <title>lean-memory console</title>
    </head>
    <body>
      <div id="root"></div>
      <script type="module" src="/src/main.tsx"></script>
    </body>
  </html>
  ```

  `ui/.gitignore`:

  ```gitignore
  node_modules/
  dist/
  bun.lockb
  *.log
  ```

  `ui/src/main.tsx`:

  ```tsx
  import React from "react";
  import ReactDOM from "react-dom/client";
  import { BrowserRouter } from "react-router-dom";
  import App from "./App";
  import "./index.css";

  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </React.StrictMode>,
  );
  ```

- [ ] **Step 6: Write `ui/src/index.css` and `ui/src/vite-env.d.ts`.**
  Tailwind v4 is a single `@import`; add a couple of base tokens the pages
  reuse. `ui/src/index.css`:

  ```css
  @import "tailwindcss";

  :root {
    color-scheme: light dark;
  }

  html,
  body,
  #root {
    height: 100%;
  }

  body {
    margin: 0;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  ```

  `ui/src/vite-env.d.ts`:

  ```ts
  /// <reference types="vite/client" />
  ```

- [ ] **Step 7: Write `ui/src/types.ts` with the §7 wire shapes.**
  These mirror the endpoint contract exactly; the column and score names come
  from the source reads noted above.

  ```ts
  export type Mode = "local" | "docker";
  export type AuthKind = "token" | "bearer";

  export interface WhoAmI {
    mode: Mode;
    auth: AuthKind;
    authenticated: boolean;
    data_root: string;
  }

  export interface Envelope<T> {
    items: T[];
    page: number;
    page_size: number;
    total: number;
  }

  export interface TopPredicate {
    predicate: string;
    count: number;
  }

  export interface NamespaceActivity {
    adds: number;
    searches: number;
    earliest_ts: number | null;
  }

  export interface NamespaceCard {
    name: string;
    facts_latest: number;
    facts_retired: number;
    entities: number;
    episodes: number;
    chains: number;
    file_size: number;
    top_predicates: TopPredicate[];
    activity: NamespaceActivity;
  }

  export interface Fact {
    fact_id: string;
    fact_text: string;
    subject: string | null;
    predicate: string;
    object_literal: string | null;
    salience: number;
    confidence: number;
    is_latest: number;
    access_count: number;
    valid_at: number;
    valid_to: number | null;
    superseded_by: string | null;
    episode_id: string;
    created_at: number;
  }

  export interface ChainLink {
    fact_id: string;
    fact_text: string;
    valid_at: number;
    valid_to: number | null;
    is_latest: number;
  }

  export interface EpisodeRef {
    id: string;
    raw: string;
    source: string | null;
    t_ref: number;
  }

  export interface FactDetail extends Fact {
    chain: ChainLink[];
    episode: EpisodeRef | null;
  }

  export interface Episode {
    id: string;
    raw: string;
    source: string | null;
    t_ref: number;
    created_at: number;
  }

  export interface EpisodeDetail extends Episode {
    facts: Fact[];
  }

  export interface Entity {
    name: string;
    fact_count: number;
  }

  export interface SearchHit {
    fact_id: string;
    fact_text: string;
    final_score: number;
    relevance: number;
    recency: number;
    importance: number;
    dense_rank: number | null;
    sparse_rank: number | null;
    rrf_score: number | null;
  }

  export interface AddPayload {
    episode_text_chars: number;
    source: string;
    t_ref: number | null;
    fact_ids: string[];
    fact_count: number;
    superseded_fact_ids: string[];
    superseded_count: number;
    origin?: string;
    error?: string;
  }

  export interface SearchPayload {
    query: string;
    k: number;
    latest_only: boolean;
    origin: string;
    hits: SearchHit[];
    error?: string;
  }

  export interface EventRow {
    id: number;
    namespace: string;
    ts: number;
    kind: "add" | "search";
    duration_ms: number;
    payload: AddPayload | SearchPayload;
  }
  ```

- [ ] **Step 8: Write `ui/src/api.ts` — the typed client with the whole auth story.**
  A module-level token/key holds the credential; `whoami()` runs first, and
  `bootLocalToken()` extracts `?token` from the URL and immediately strips it
  via `history.replaceState`. Local sends `X-Console-Token`; docker sends
  `Authorization: Bearer`. Every endpoint function matches §7 exactly, and
  `ApiError` carries the HTTP status so `App.tsx` can branch on 401.

  ```ts
  import type {
    WhoAmI,
    Envelope,
    NamespaceCard,
    Fact,
    FactDetail,
    Episode,
    EpisodeDetail,
    Entity,
    EventRow,
    SearchHit,
  } from "./types";

  export class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }

  let sessionToken: string | null = null;
  let bearerKey: string | null = null;

  /** Local mode: pull ?token from the URL, keep it in module memory, and strip
   *  it from the address bar so it never leaks via Referer or bookmarks. */
  export function bootLocalToken(): void {
    const params = new URLSearchParams(window.location.search);
    const t = params.get("token");
    if (t) {
      sessionToken = t;
      params.delete("token");
      const qs = params.toString();
      const clean = window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash;
      window.history.replaceState(null, "", clean);
    }
  }

  /** Docker mode: store the key entered on the login screen (React context only). */
  export function setBearerKey(key: string): void {
    bearerKey = key;
  }

  function authHeaders(): Record<string, string> {
    const h: Record<string, string> = {};
    if (sessionToken) h["X-Console-Token"] = sessionToken;
    if (bearerKey) h["Authorization"] = `Bearer ${bearerKey}`;
    return h;
  }

  async function req<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(path, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...authHeaders(),
        ...(init?.headers ?? {}),
      },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = (body && (body.detail || body.message)) || detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(res.status, detail);
    }
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  function qp(params: Record<string, string | number | boolean | undefined | null>): string {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      sp.set(k, String(v));
    }
    const s = sp.toString();
    return s ? `?${s}` : "";
  }

  // ── §7 endpoints ────────────────────────────────────────────────────────

  export function whoami(): Promise<WhoAmI> {
    return req<WhoAmI>("/views/whoami");
  }

  export function listNamespaces(): Promise<NamespaceCard[]> {
    return req<NamespaceCard[]>("/views/namespaces");
  }

  export interface FactFilters {
    latest_only?: boolean;
    predicate?: string;
    entity?: string;
    min_salience?: number;
    q?: string;
    page?: number;
    page_size?: number;
  }

  export function listFacts(ns: string, f: FactFilters = {}): Promise<Envelope<Fact>> {
    return req<Envelope<Fact>>(`/views/${encodeURIComponent(ns)}/facts${qp({ ...f })}`);
  }

  export function getFact(ns: string, factId: string): Promise<FactDetail> {
    return req<FactDetail>(
      `/views/${encodeURIComponent(ns)}/facts/${encodeURIComponent(factId)}`,
    );
  }

  export function listEpisodes(
    ns: string,
    page = 1,
    pageSize = 50,
  ): Promise<Envelope<Episode>> {
    return req<Envelope<Episode>>(
      `/views/${encodeURIComponent(ns)}/episodes${qp({ page, page_size: pageSize })}`,
    );
  }

  export function getEpisode(ns: string, episodeId: string): Promise<EpisodeDetail> {
    return req<EpisodeDetail>(
      `/views/${encodeURIComponent(ns)}/episodes/${encodeURIComponent(episodeId)}`,
    );
  }

  export function listEntities(
    ns: string,
    page = 1,
    pageSize = 50,
  ): Promise<Envelope<Entity>> {
    return req<Envelope<Entity>>(
      `/views/${encodeURIComponent(ns)}/entities${qp({ page, page_size: pageSize })}`,
    );
  }

  export function listEvents(
    ns: string,
    kind?: "add" | "search",
    page = 1,
    pageSize = 50,
  ): Promise<Envelope<EventRow>> {
    return req<Envelope<EventRow>>(
      `/views/${encodeURIComponent(ns)}/events${qp({ kind, page, page_size: pageSize })}`,
    );
  }

  export function testSearch(
    ns: string,
    query: string,
    k: number,
  ): Promise<{ hits: SearchHit[]; duration_ms: number }> {
    return req<{ hits: SearchHit[]; duration_ms: number }>(
      `/views/${encodeURIComponent(ns)}/test-search`,
      { method: "POST", body: JSON.stringify({ query, k }) },
    );
  }
  ```

- [ ] **Step 9: Write `ui/src/auth.tsx` — the auth context + boot orchestration.**
  The provider calls `whoami()` on mount (after `bootLocalToken()`), then
  exposes `{status, whoami, error, login}`. `login(key)` sets the bearer key
  and re-probes. This is the single source of truth `App.tsx` branches on.

  ```tsx
  import React, {
    createContext,
    useCallback,
    useContext,
    useEffect,
    useState,
  } from "react";
  import type { WhoAmI } from "./types";
  import { ApiError, bootLocalToken, setBearerKey, whoami as probeWhoami } from "./api";

  type Status = "loading" | "ready" | "needs-login" | "error";

  interface AuthState {
    status: Status;
    whoami: WhoAmI | null;
    error: string | null;
    login: (key: string) => Promise<void>;
  }

  const AuthContext = createContext<AuthState | null>(null);

  export function AuthProvider({ children }: { children: React.ReactNode }) {
    const [status, setStatus] = useState<Status>("loading");
    const [who, setWho] = useState<WhoAmI | null>(null);
    const [error, setError] = useState<string | null>(null);

    const probe = useCallback(async () => {
      try {
        const w = await probeWhoami();
        setWho(w);
        if (w.authenticated) {
          setStatus("ready");
          setError(null);
        } else if (w.mode === "docker") {
          setStatus("needs-login");
        } else {
          // local + unauthenticated: no login screen, plain error (§7).
          setStatus("error");
          setError("No valid session token. Re-open the console from the URL it printed.");
        }
      } catch (e) {
        const msg = e instanceof ApiError ? `whoami failed (${e.status})` : "whoami failed";
        setStatus("error");
        setError(msg);
      }
    }, []);

    useEffect(() => {
      bootLocalToken();
      void probe();
    }, [probe]);

    const login = useCallback(
      async (key: string) => {
        setBearerKey(key);
        setStatus("loading");
        await probe();
      },
      [probe],
    );

    return (
      <AuthContext.Provider value={{ status, whoami: who, error, login }}>
        {children}
      </AuthContext.Provider>
    );
  }

  export function useAuth(): AuthState {
    const ctx = useContext(AuthContext);
    if (!ctx) throw new Error("useAuth must be used within AuthProvider");
    return ctx;
  }
  ```

- [ ] **Step 10: Write the four page stubs.**
  Each is a real component that Tasks 11-14 replace wholesale; the stub keeps
  `App.tsx` type-clean at this task's gate. Each takes the active namespace.

  `ui/src/pages/Overview.tsx`:

  ```tsx
  export default function Overview() {
    return <div className="p-6 text-sm text-slate-500">Overview (Task 11)</div>;
  }
  ```

  `ui/src/pages/Memories.tsx`:

  ```tsx
  export default function Memories({ ns }: { ns: string }) {
    return (
      <div className="p-6 text-sm text-slate-500">Memories for {ns} (Task 12)</div>
    );
  }
  ```

  `ui/src/pages/Episodes.tsx`:

  ```tsx
  export default function Episodes({ ns }: { ns: string }) {
    return (
      <div className="p-6 text-sm text-slate-500">Episodes for {ns} (Task 13)</div>
    );
  }
  ```

  `ui/src/pages/Activity.tsx`:

  ```tsx
  export default function Activity({ ns }: { ns: string }) {
    return (
      <div className="p-6 text-sm text-slate-500">Activity for {ns} (Task 14)</div>
    );
  }
  ```

- [ ] **Step 11: Write `ui/src/components/Layout.tsx` — header shell + namespace switcher.**
  The header holds the nav links (react-router `NavLink`), the namespace
  `<select>` (fed from `/views/namespaces`), and the resolved data root from
  whoami. When there are no namespaces, it still renders (Overview handles the
  empty state / connect snippets).

  ```tsx
  import { NavLink } from "react-router-dom";
  import type { NamespaceCard, WhoAmI } from "../types";

  const navItems = [
    { to: "/", label: "Overview", end: true },
    { to: "/memories", label: "Memories", end: false },
    { to: "/episodes", label: "Episodes", end: false },
    { to: "/activity", label: "Activity", end: false },
  ];

  export default function Layout({
    who,
    namespaces,
    activeNs,
    onNsChange,
    children,
  }: {
    who: WhoAmI;
    namespaces: NamespaceCard[];
    activeNs: string | null;
    onNsChange: (ns: string) => void;
    children: React.ReactNode;
  }) {
    return (
      <div className="min-h-full bg-slate-50 text-slate-900">
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-6 py-3">
            <span className="text-sm font-semibold tracking-tight">
              lean-memory console
            </span>
            <nav className="flex gap-1">
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    `rounded px-3 py-1.5 text-sm ${
                      isActive
                        ? "bg-slate-900 text-white"
                        : "text-slate-600 hover:bg-slate-100"
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
            <div className="ml-auto flex items-center gap-3">
              <label className="text-xs text-slate-500">namespace</label>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1 text-sm"
                value={activeNs ?? ""}
                onChange={(e) => onNsChange(e.target.value)}
                disabled={namespaces.length === 0}
              >
                {namespaces.length === 0 && <option value="">no namespaces</option>}
                {namespaces.map((n) => (
                  <option key={n.name} value={n.name}>
                    {n.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="mx-auto max-w-6xl px-6 pb-2 text-xs text-slate-400">
            {who.mode} mode · root {who.data_root}
          </div>
        </header>
        <main className="mx-auto max-w-6xl">{children}</main>
      </div>
    );
  }
  ```

- [ ] **Step 12: Write `ui/src/App.tsx` — router shell wired to auth + namespaces.**
  `App` wraps everything in `AuthProvider`; the inner `Shell` branches on auth
  status (loading / login screen for docker / plain error for local / ready),
  then loads namespaces once ready, tracks the active namespace, and renders
  the routed pages inside `Layout`. Reserved / empty namespace list still
  renders (pages own their own empty states).

  ```tsx
  import { useEffect, useState } from "react";
  import { Route, Routes } from "react-router-dom";
  import { AuthProvider, useAuth } from "./auth";
  import { listNamespaces } from "./api";
  import type { NamespaceCard } from "./types";
  import Layout from "./components/Layout";
  import Overview from "./pages/Overview";
  import Memories from "./pages/Memories";
  import Episodes from "./pages/Episodes";
  import Activity from "./pages/Activity";

  function LoginScreen() {
    const { login, error } = useAuth();
    const [key, setKey] = useState("");
    const [submitting, setSubmitting] = useState(false);
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50">
        <form
          className="w-80 space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
          onSubmit={async (e) => {
            e.preventDefault();
            setSubmitting(true);
            try {
              await login(key.trim());
            } finally {
              setSubmitting(false);
            }
          }}
        >
          <h1 className="text-sm font-semibold">lean-memory console</h1>
          <p className="text-xs text-slate-500">
            Enter the API key (LM_API_KEY) for this container.
          </p>
          <input
            type="password"
            autoFocus
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="API key"
            className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
          />
          {error && <p className="text-xs text-red-600">{error}</p>}
          <button
            type="submit"
            disabled={submitting || key.trim() === ""}
            className="w-full rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-40"
          >
            {submitting ? "Checking…" : "Connect"}
          </button>
        </form>
      </div>
    );
  }

  function ErrorScreen({ message }: { message: string }) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
        <div className="max-w-md rounded-lg border border-red-200 bg-white p-6 text-center shadow-sm">
          <h1 className="text-sm font-semibold text-red-700">Cannot connect</h1>
          <p className="mt-2 text-xs text-slate-600">{message}</p>
        </div>
      </div>
    );
  }

  function Shell() {
    const { status, whoami, error } = useAuth();
    const [namespaces, setNamespaces] = useState<NamespaceCard[]>([]);
    const [activeNs, setActiveNs] = useState<string | null>(null);
    const [nsError, setNsError] = useState<string | null>(null);

    useEffect(() => {
      if (status !== "ready") return;
      let cancelled = false;
      listNamespaces()
        .then((ns) => {
          if (cancelled) return;
          setNamespaces(ns);
          setActiveNs((prev) => prev ?? (ns.length ? ns[0].name : null));
        })
        .catch((e) => !cancelled && setNsError(String(e)));
      return () => {
        cancelled = true;
      };
    }, [status]);

    if (status === "loading") {
      return (
        <div className="flex min-h-screen items-center justify-center text-sm text-slate-400">
          Loading…
        </div>
      );
    }
    if (status === "needs-login") return <LoginScreen />;
    if (status === "error" || !whoami) {
      return <ErrorScreen message={error ?? "Unknown error"} />;
    }

    const ns = activeNs ?? "";
    return (
      <Layout
        who={whoami}
        namespaces={namespaces}
        activeNs={activeNs}
        onNsChange={setActiveNs}
      >
        {nsError && (
          <div className="p-4 text-sm text-red-600">
            Failed to list namespaces: {nsError}
          </div>
        )}
        <Routes>
          <Route
            path="/"
            element={<Overview who={whoami} namespaces={namespaces} />}
          />
          <Route path="/memories" element={<Memories ns={ns} />} />
          <Route path="/episodes" element={<Episodes ns={ns} />} />
          <Route path="/activity" element={<Activity ns={ns} />} />
        </Routes>
      </Layout>
    );
  }

  export default function App() {
    return (
      <AuthProvider>
        <Shell />
      </AuthProvider>
    );
  }
  ```

  Note: the `Overview` stub from Step 10 takes no props; update its signature
  in this step to accept `{ who, namespaces }` so `App.tsx` typechecks — since
  Task 11 replaces the file wholesale, write the stub now as:

  ```tsx
  import type { NamespaceCard, WhoAmI } from "../types";

  export default function Overview(_props: {
    who: WhoAmI;
    namespaces: NamespaceCard[];
  }) {
    return <div className="p-6 text-sm text-slate-500">Overview (Task 11)</div>;
  }
  ```

- [ ] **Step 13: Install deps and run the gate.**
  From `ui/`:

  ```bash
  cd ui && bun install
  bun run typecheck && bun run build
  ```

  Expected: `tsc --noEmit` exits 0; `vite build` writes assets and
  `console/src/lean_memory_console/static/index.html` exists. Verify:

  ```bash
  test -f /Users/wuesteon/research/lean-memory/.claude/worktrees/memory-ui/console/src/lean_memory_console/static/index.html && echo "OK: static/index.html present"
  ```

  Expected: prints `OK: static/index.html present`.

- [ ] **Step 14: Commit.**

  ```bash
  git add ui/package.json ui/tsconfig.json ui/tsconfig.node.json ui/vite.config.ts \
    ui/index.html ui/.gitignore ui/src/main.tsx ui/src/index.css ui/src/vite-env.d.ts \
    ui/src/api.ts ui/src/types.ts ui/src/auth.tsx ui/src/App.tsx \
    ui/src/components/Layout.tsx ui/src/pages/Overview.tsx ui/src/pages/Memories.tsx \
    ui/src/pages/Episodes.tsx ui/src/pages/Activity.tsx console/.gitignore
  git commit -m "feat(ui): scaffold React SPA with typed API client, auth, router shell"
  ```

  Note: `bun.lockb` is gitignored (Step 5) so it is not added; `ui/src/pages/*`
  paths above are the stubs — Tasks 11-14 modify them in place.

---

### Task 11: Overview page

Namespace cards per §8.1: counts, top predicates, a 7-day adds/searches
sparkline from the `activity` fields, supersession rate = `retired /
(latest + retired)` and facts-per-add as plain numbers, and an earliest-event
truncation note. When there are zero namespaces, the page *is* the connect
snippets (observing-MCP line, plugin install, and the Docker snippet keyed off
`whoami.mode`).

**Files:**
- Modify: `ui/src/pages/Overview.tsx`
- Create: `ui/src/components/Sparkline.tsx`
- Create: `ui/src/components/ConnectSnippets.tsx`
- Create: `ui/src/lib/format.ts`
- Test (gate): `bun run typecheck && bun run build` from `ui/`

**Interfaces:**
- Consumes: `GET /views/namespaces` (already loaded by `App.tsx` and passed in
  as the `namespaces` prop), `whoami.mode` for the Docker snippet.
- Produces: the rendered Overview; no new API calls.

- [ ] **Step 1: Write `ui/src/lib/format.ts` — shared formatting helpers.**
  Byte and count formatting plus the two derived ratios and a truncation-note
  helper, so cards and later pages share one implementation.

  ```ts
  export function formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB"];
    let v = bytes / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i += 1;
    }
    return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
  }

  export function formatCount(n: number): string {
    return n.toLocaleString();
  }

  /** retired / (latest + retired); 0 when there are no facts. */
  export function supersessionRate(latest: number, retired: number): number {
    const total = latest + retired;
    return total === 0 ? 0 : retired / total;
  }

  export function formatPct(x: number): string {
    return `${(x * 100).toFixed(1)}%`;
  }

  /** (latest + retired) / adds; null when adds == 0 (unknown, not zero). */
  export function factsPerAdd(latest: number, retired: number, adds: number): number | null {
    if (adds === 0) return null;
    return (latest + retired) / adds;
  }

  export function formatTs(ms: number): string {
    return new Date(ms).toLocaleString();
  }
  ```

- [ ] **Step 2: Write `ui/src/components/Sparkline.tsx`.**
  A minimal Recharts line for the 7-day adds/searches. The `activity` fields
  are totals (adds/searches over 7 days), so the sparkline plots the two
  totals as a tiny two-point comparison line — Recharts is used *sparingly*
  per §8. Guards against empty data.

  ```tsx
  import { Line, LineChart, ResponsiveContainer, Tooltip } from "recharts";

  export default function Sparkline({
    adds,
    searches,
  }: {
    adds: number;
    searches: number;
  }) {
    const data = [
      { label: "adds", value: adds },
      { label: "searches", value: searches },
    ];
    if (adds === 0 && searches === 0) {
      return <div className="h-8 text-xs text-slate-400">no activity (7d)</div>;
    }
    return (
      <div className="h-8 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <Tooltip
              cursor={false}
              contentStyle={{ fontSize: 11 }}
              formatter={(v: number, _n, p) => [v, p.payload.label]}
            />
            <Line
              type="monotone"
              dataKey="value"
              stroke="#0f172a"
              strokeWidth={1.5}
              dot={{ r: 2 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    );
  }
  ```

- [ ] **Step 3: Write `ui/src/components/ConnectSnippets.tsx`.**
  The empty-state and Docker-mode connect block: observing-MCP line, plugin
  install commands, and the Docker `claude mcp add` snippet (shown when
  `mode === "docker"`, per §6). Uses `<pre>` blocks with copy-friendly text.

  ```tsx
  import type { Mode } from "../types";

  function Snippet({ title, code }: { title: string; code: string }) {
    return (
      <div className="space-y-1">
        <div className="text-xs font-medium text-slate-600">{title}</div>
        <pre className="overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
          <code>{code}</code>
        </pre>
      </div>
    );
  }

  export default function ConnectSnippets({
    mode,
    dataRoot,
  }: {
    mode: Mode;
    dataRoot: string;
  }) {
    return (
      <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-6">
        <div>
          <h2 className="text-sm font-semibold">No memories yet</h2>
          <p className="mt-1 text-xs text-slate-500">
            Connect an agent to write memories into{" "}
            <code className="rounded bg-slate-100 px-1">{dataRoot}</code>. The
            console is read-only over whatever your agent stores.
          </p>
        </div>
        <Snippet
          title="Install the Claude Code plugin"
          code={
            "/plugin marketplace add <owner>/lean-memory-console\n" +
            "/plugin install lean-memory"
          }
        />
        <Snippet
          title="Or add the observing MCP directly"
          code={"claude mcp add lean-memory -- uvx lean-memory-console mcp"}
        />
        {mode === "docker" && (
          <Snippet
            title="Docker (HTTP MCP)"
            code={
              "claude mcp add --transport http lean-memory http://<host>:8377/mcp \\\n" +
              '  --header "Authorization: Bearer $LM_API_KEY"'
            }
          />
        )}
      </div>
    );
  }
  ```

- [ ] **Step 4: Write the real `ui/src/pages/Overview.tsx`.**
  Renders one card per namespace with counts, top predicates, the sparkline,
  the two derived ratios as plain numbers, and the earliest-event note. Empty
  → `ConnectSnippets`.

  ```tsx
  import type { NamespaceCard, WhoAmI } from "../types";
  import Sparkline from "../components/Sparkline";
  import ConnectSnippets from "../components/ConnectSnippets";
  import {
    factsPerAdd,
    formatBytes,
    formatCount,
    formatPct,
    formatTs,
    supersessionRate,
  } from "../lib/format";

  function Stat({ label, value }: { label: string; value: string }) {
    return (
      <div>
        <div className="text-lg font-semibold tabular-nums">{value}</div>
        <div className="text-xs text-slate-500">{label}</div>
      </div>
    );
  }

  function Card({ ns }: { ns: NamespaceCard }) {
    const rate = supersessionRate(ns.facts_latest, ns.facts_retired);
    const fpa = factsPerAdd(ns.facts_latest, ns.facts_retired, ns.activity.adds);
    return (
      <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex items-baseline justify-between">
          <h3 className="text-sm font-semibold">{ns.name}</h3>
          <span className="text-xs text-slate-400">{formatBytes(ns.file_size)}</span>
        </div>

        <div className="grid grid-cols-4 gap-3">
          <Stat label="facts (latest)" value={formatCount(ns.facts_latest)} />
          <Stat label="retired" value={formatCount(ns.facts_retired)} />
          <Stat label="episodes" value={formatCount(ns.episodes)} />
          <Stat label="entities" value={formatCount(ns.entities)} />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Stat label="supersession rate" value={formatPct(rate)} />
          <Stat
            label="facts / add"
            value={fpa === null ? "—" : fpa.toFixed(1)}
          />
        </div>

        <div>
          <div className="mb-1 text-xs font-medium text-slate-600">
            top predicates
          </div>
          <div className="flex flex-wrap gap-1">
            {ns.top_predicates.length === 0 && (
              <span className="text-xs text-slate-400">none</span>
            )}
            {ns.top_predicates.map((p) => (
              <span
                key={p.predicate}
                className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700"
              >
                {p.predicate} · {p.count}
              </span>
            ))}
          </div>
        </div>

        <div>
          <div className="mb-1 flex items-center justify-between text-xs text-slate-600">
            <span>activity (7d)</span>
            <span className="text-slate-400">
              {ns.activity.adds} adds · {ns.activity.searches} searches
            </span>
          </div>
          <Sparkline adds={ns.activity.adds} searches={ns.activity.searches} />
          {ns.activity.earliest_ts !== null && (
            <div className="mt-1 text-[11px] text-slate-400">
              events retained from {formatTs(ns.activity.earliest_ts)} (older
              events pruned at the 10k cap)
            </div>
          )}
        </div>
      </div>
    );
  }

  export default function Overview({
    who,
    namespaces,
  }: {
    who: WhoAmI;
    namespaces: NamespaceCard[];
  }) {
    if (namespaces.length === 0) {
      return (
        <div className="p-6">
          <ConnectSnippets mode={who.mode} dataRoot={who.data_root} />
        </div>
      );
    }
    return (
      <div className="grid gap-4 p-6 md:grid-cols-2">
        {namespaces.map((ns) => (
          <Card key={ns.name} ns={ns} />
        ))}
      </div>
    );
  }
  ```

- [ ] **Step 5: Run the gate.**

  ```bash
  cd ui && bun run typecheck && bun run build
  ```

  Expected: PASS — `tsc --noEmit` exits 0, `vite build` succeeds, no unused
  locals/params errors.

- [ ] **Step 6: Commit.**

  ```bash
  git add ui/src/pages/Overview.tsx ui/src/components/Sparkline.tsx \
    ui/src/components/ConnectSnippets.tsx ui/src/lib/format.ts
  git commit -m "feat(ui): Overview page with namespace cards, sparkline, connect snippets"
  ```

---

### Task 12: Memories page

The filterable fact table (§8.2) with all columns, a filter bar wired to
`/views/{ns}/facts` params (`latest_only` toggle default on, `predicate`
select sourced from the namespace's `top_predicates`, `entity` text,
`min_salience` number, `q` text), pagination driven by the envelope `total`,
and a fact drawer showing full metadata, the supersession timeline (vertical
chain oldest→newest with `valid_at`/`valid_to` intervals and an `is_latest`
badge), and the provenance episode block.

**Files:**
- Modify: `ui/src/pages/Memories.tsx`
- Create: `ui/src/components/Pagination.tsx`
- Create: `ui/src/components/FactDrawer.tsx`
- Test (gate): `bun run typecheck && bun run build` from `ui/`

**Interfaces:**
- Consumes: `GET /views/{ns}/facts?...` (envelope of `Fact`),
  `GET /views/{ns}/facts/{fact_id}` (`FactDetail` with `chain` + `episode`),
  `GET /views/namespaces` (for the active namespace's `top_predicates` to
  populate the predicate select).
- Produces: the rendered Memories page + drawer.

- [ ] **Step 1: Write `ui/src/components/Pagination.tsx`.**
  Reusable prev/next controls computing page count from `total` and
  `page_size`; used by Memories, Episodes, and Activity.

  ```tsx
  export default function Pagination({
    page,
    pageSize,
    total,
    onPage,
  }: {
    page: number;
    pageSize: number;
    total: number;
    onPage: (p: number) => void;
  }) {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    return (
      <div className="flex items-center gap-3 text-sm">
        <button
          className="rounded border border-slate-300 px-2 py-1 disabled:opacity-40"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
        >
          Prev
        </button>
        <span className="text-slate-500">
          page {page} / {pages} · {total.toLocaleString()} total
        </span>
        <button
          className="rounded border border-slate-300 px-2 py-1 disabled:opacity-40"
          disabled={page >= pages}
          onClick={() => onPage(page + 1)}
        >
          Next
        </button>
      </div>
    );
  }
  ```

- [ ] **Step 2: Write `ui/src/components/FactDrawer.tsx`.**
  Fetches `getFact(ns, factId)` on open, renders full metadata, the vertical
  supersession timeline (chain oldest→newest; each link shows its valid
  interval and an `is_latest` badge; the currently-open fact is highlighted),
  and the provenance episode block.

  ```tsx
  import { useEffect, useState } from "react";
  import { getFact } from "../api";
  import type { FactDetail } from "../types";
  import { formatTs } from "../lib/format";

  function interval(validAt: number, validTo: number | null): string {
    return `${formatTs(validAt)} → ${validTo === null ? "now" : formatTs(validTo)}`;
  }

  export default function FactDrawer({
    ns,
    factId,
    onClose,
  }: {
    ns: string;
    factId: string;
    onClose: () => void;
  }) {
    const [detail, setDetail] = useState<FactDetail | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
      let cancelled = false;
      setDetail(null);
      setError(null);
      getFact(ns, factId)
        .then((d) => !cancelled && setDetail(d))
        .catch((e) => !cancelled && setError(String(e)));
      return () => {
        cancelled = true;
      };
    }, [ns, factId]);

    return (
      <div className="fixed inset-0 z-40 flex justify-end">
        <div
          className="absolute inset-0 bg-black/30"
          onClick={onClose}
          aria-hidden
        />
        <div className="relative z-50 h-full w-full max-w-lg overflow-y-auto bg-white p-6 shadow-xl">
          <div className="flex items-start justify-between">
            <h2 className="text-sm font-semibold">Fact detail</h2>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-xs"
              onClick={onClose}
            >
              Close
            </button>
          </div>

          {error && <p className="mt-4 text-sm text-red-600">{error}</p>}
          {!detail && !error && (
            <p className="mt-4 text-sm text-slate-400">Loading…</p>
          )}

          {detail && (
            <div className="mt-4 space-y-6">
              <p className="text-sm">{detail.fact_text}</p>

              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
                <dt className="text-slate-500">subject</dt>
                <dd>{detail.subject ?? "—"}</dd>
                <dt className="text-slate-500">predicate</dt>
                <dd>{detail.predicate}</dd>
                <dt className="text-slate-500">object</dt>
                <dd>{detail.object_literal ?? "—"}</dd>
                <dt className="text-slate-500">salience</dt>
                <dd className="tabular-nums">{detail.salience.toFixed(2)}</dd>
                <dt className="text-slate-500">confidence</dt>
                <dd className="tabular-nums">{detail.confidence.toFixed(2)}</dd>
                <dt className="text-slate-500">access count</dt>
                <dd className="tabular-nums">{detail.access_count}</dd>
                <dt className="text-slate-500">is latest</dt>
                <dd>{detail.is_latest ? "yes" : "no"}</dd>
                <dt className="text-slate-500">valid</dt>
                <dd>{interval(detail.valid_at, detail.valid_to)}</dd>
              </dl>

              <div>
                <h3 className="mb-2 text-xs font-semibold text-slate-600">
                  Supersession timeline
                </h3>
                <ol className="space-y-3 border-l-2 border-slate-200 pl-4">
                  {detail.chain.map((link) => (
                    <li key={link.fact_id} className="relative">
                      <span
                        className={`absolute -left-[21px] top-1 h-3 w-3 rounded-full border-2 ${
                          link.fact_id === detail.fact_id
                            ? "border-slate-900 bg-slate-900"
                            : "border-slate-300 bg-white"
                        }`}
                      />
                      <div className="flex items-center gap-2">
                        <span className="text-xs">{link.fact_text}</span>
                        {link.is_latest === 1 && (
                          <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                            latest
                          </span>
                        )}
                      </div>
                      <div className="text-[11px] text-slate-400">
                        {interval(link.valid_at, link.valid_to)}
                      </div>
                    </li>
                  ))}
                  {detail.chain.length === 0 && (
                    <li className="text-xs text-slate-400">
                      no supersession chain (standalone fact)
                    </li>
                  )}
                </ol>
              </div>

              <div>
                <h3 className="mb-2 text-xs font-semibold text-slate-600">
                  Provenance episode
                </h3>
                {detail.episode ? (
                  <div className="rounded border border-slate-200 bg-slate-50 p-3">
                    <div className="text-[11px] text-slate-400">
                      {detail.episode.source ?? "unknown"} ·{" "}
                      {formatTs(detail.episode.t_ref)}
                    </div>
                    <p className="mt-1 whitespace-pre-wrap text-xs">
                      {detail.episode.raw}
                    </p>
                  </div>
                ) : (
                  <p className="text-xs text-slate-400">episode unavailable</p>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }
  ```

- [ ] **Step 3: Write the real `ui/src/pages/Memories.tsx`.**
  Filter bar, fact table with every §8.2 column, envelope-driven pagination,
  and the drawer. The predicate select is sourced from the active namespace's
  `top_predicates`, so the page fetches `/views/namespaces` once to find it.
  Filters reset the page to 1 on change.

  ```tsx
  import { useCallback, useEffect, useState } from "react";
  import { listFacts, listNamespaces } from "../api";
  import type { Fact, TopPredicate } from "../types";
  import type { FactFilters } from "../api";
  import Pagination from "../components/Pagination";
  import FactDrawer from "../components/FactDrawer";
  import { formatTs } from "../lib/format";

  const PAGE_SIZE = 50;

  interface FilterState {
    latest_only: boolean;
    predicate: string;
    entity: string;
    min_salience: string;
    q: string;
  }

  const EMPTY: FilterState = {
    latest_only: true,
    predicate: "",
    entity: "",
    min_salience: "",
    q: "",
  };

  export default function Memories({ ns }: { ns: string }) {
    const [filters, setFilters] = useState<FilterState>(EMPTY);
    const [applied, setApplied] = useState<FilterState>(EMPTY);
    const [page, setPage] = useState(1);
    const [rows, setRows] = useState<Fact[]>([]);
    const [total, setTotal] = useState(0);
    const [predicates, setPredicates] = useState<TopPredicate[]>([]);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [openFact, setOpenFact] = useState<string | null>(null);

    useEffect(() => {
      // reset when the namespace changes
      setFilters(EMPTY);
      setApplied(EMPTY);
      setPage(1);
    }, [ns]);

    useEffect(() => {
      if (!ns) return;
      let cancelled = false;
      listNamespaces()
        .then((all) => {
          if (cancelled) return;
          const card = all.find((c) => c.name === ns);
          setPredicates(card ? card.top_predicates : []);
        })
        .catch(() => setPredicates([]));
      return () => {
        cancelled = true;
      };
    }, [ns]);

    const load = useCallback(() => {
      if (!ns) return;
      setLoading(true);
      setError(null);
      const params: FactFilters = {
        latest_only: applied.latest_only,
        predicate: applied.predicate || undefined,
        entity: applied.entity || undefined,
        min_salience:
          applied.min_salience === "" ? undefined : Number(applied.min_salience),
        q: applied.q || undefined,
        page,
        page_size: PAGE_SIZE,
      };
      listFacts(ns, params)
        .then((env) => {
          setRows(env.items);
          setTotal(env.total);
        })
        .catch((e) => setError(String(e)))
        .finally(() => setLoading(false));
    }, [ns, applied, page]);

    useEffect(() => {
      load();
    }, [load]);

    function applyFilters() {
      setApplied(filters);
      setPage(1);
    }

    if (!ns) {
      return <div className="p-6 text-sm text-slate-500">No namespace selected.</div>;
    }

    return (
      <div className="space-y-4 p-6">
        <div className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-200 bg-white p-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={filters.latest_only}
              onChange={(e) =>
                setFilters((f) => ({ ...f, latest_only: e.target.checked }))
              }
            />
            latest only
          </label>
          <label className="flex flex-col text-xs text-slate-500">
            predicate
            <select
              className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
              value={filters.predicate}
              onChange={(e) =>
                setFilters((f) => ({ ...f, predicate: e.target.value }))
              }
            >
              <option value="">any</option>
              {predicates.map((p) => (
                <option key={p.predicate} value={p.predicate}>
                  {p.predicate}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col text-xs text-slate-500">
            entity
            <input
              className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
              value={filters.entity}
              onChange={(e) =>
                setFilters((f) => ({ ...f, entity: e.target.value }))
              }
              placeholder="name"
            />
          </label>
          <label className="flex flex-col text-xs text-slate-500">
            min salience
            <input
              type="number"
              step="0.1"
              min="0"
              max="10"
              className="mt-1 w-24 rounded border border-slate-300 px-2 py-1 text-sm"
              value={filters.min_salience}
              onChange={(e) =>
                setFilters((f) => ({ ...f, min_salience: e.target.value }))
              }
            />
          </label>
          <label className="flex flex-col text-xs text-slate-500">
            text (FTS)
            <input
              className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
              value={filters.q}
              onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
              placeholder="match text"
            />
          </label>
          <button
            className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white"
            onClick={applyFilters}
          >
            Apply
          </button>
        </div>

        {error && <p className="text-sm text-red-600">{error}</p>}

        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs text-slate-500">
              <tr>
                <th className="px-3 py-2">fact_text</th>
                <th className="px-3 py-2">subject</th>
                <th className="px-3 py-2">predicate</th>
                <th className="px-3 py-2">object</th>
                <th className="px-3 py-2 text-right">salience</th>
                <th className="px-3 py-2 text-right">conf</th>
                <th className="px-3 py-2">latest</th>
                <th className="px-3 py-2 text-right">access</th>
                <th className="px-3 py-2">valid_at</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.fact_id}
                  className="cursor-pointer border-t border-slate-100 hover:bg-slate-50"
                  onClick={() => setOpenFact(r.fact_id)}
                >
                  <td className="max-w-xs truncate px-3 py-2">{r.fact_text}</td>
                  <td className="px-3 py-2">{r.subject ?? "—"}</td>
                  <td className="px-3 py-2">{r.predicate}</td>
                  <td className="px-3 py-2">{r.object_literal ?? "—"}</td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {r.salience.toFixed(1)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {r.confidence.toFixed(2)}
                  </td>
                  <td className="px-3 py-2">
                    {r.is_latest ? (
                      <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-700">
                        yes
                      </span>
                    ) : (
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                        no
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {r.access_count}
                  </td>
                  <td className="px-3 py-2 text-xs text-slate-500">
                    {formatTs(r.valid_at)}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && !loading && (
                <tr>
                  <td colSpan={9} className="px-3 py-6 text-center text-slate-400">
                    no facts match these filters
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <Pagination
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
          onPage={setPage}
        />

        {openFact && (
          <FactDrawer ns={ns} factId={openFact} onClose={() => setOpenFact(null)} />
        )}
      </div>
    );
  }
  ```

- [ ] **Step 4: Run the gate.**

  ```bash
  cd ui && bun run typecheck && bun run build
  ```

  Expected: PASS.

- [ ] **Step 5: Commit.**

  ```bash
  git add ui/src/pages/Memories.tsx ui/src/components/Pagination.tsx \
    ui/src/components/FactDrawer.tsx
  git commit -m "feat(ui): Memories page — fact table, filters, pagination, fact drawer"
  ```

---

### Task 13: Episodes page

The transcript list (`episode.raw`, ordered by `t_ref` DESC via the endpoint),
with per-episode expansion that fetches and shows the facts extracted from
that episode (the granularity window). Pagination reuses the envelope control.

**Files:**
- Modify: `ui/src/pages/Episodes.tsx`
- Create: `ui/src/components/EpisodeRow.tsx`
- Test (gate): `bun run typecheck && bun run build` from `ui/`

**Interfaces:**
- Consumes: `GET /views/{ns}/episodes?page` (envelope of `Episode`),
  `GET /views/{ns}/episodes/{id}` (`EpisodeDetail` with `facts`).
- Produces: the rendered Episodes page.

- [ ] **Step 1: Write `ui/src/components/EpisodeRow.tsx`.**
  A collapsible row: shows `t_ref`, source, and a truncated `raw`; on expand,
  lazily fetches `getEpisode` and lists the extracted facts.

  ```tsx
  import { useState } from "react";
  import { getEpisode } from "../api";
  import type { Episode, Fact } from "../types";
  import { formatTs } from "../lib/format";

  export default function EpisodeRow({
    ns,
    episode,
  }: {
    ns: string;
    episode: Episode;
  }) {
    const [open, setOpen] = useState(false);
    const [facts, setFacts] = useState<Fact[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);

    function toggle() {
      const next = !open;
      setOpen(next);
      if (next && facts === null && !loading) {
        setLoading(true);
        setError(null);
        getEpisode(ns, episode.id)
          .then((d) => setFacts(d.facts))
          .catch((e) => setError(String(e)))
          .finally(() => setLoading(false));
      }
    }

    return (
      <div className="rounded-lg border border-slate-200 bg-white">
        <button
          className="flex w-full items-start gap-3 px-4 py-3 text-left"
          onClick={toggle}
        >
          <span className="mt-0.5 text-slate-400">{open ? "▾" : "▸"}</span>
          <div className="min-w-0 flex-1">
            <div className="text-[11px] text-slate-400">
              {episode.source ?? "unknown"} · {formatTs(episode.t_ref)}
            </div>
            <p
              className={`text-sm ${open ? "whitespace-pre-wrap" : "truncate"}`}
            >
              {episode.raw}
            </p>
          </div>
        </button>

        {open && (
          <div className="border-t border-slate-100 px-4 py-3">
            <div className="mb-2 text-xs font-semibold text-slate-600">
              Extracted facts
            </div>
            {loading && <p className="text-xs text-slate-400">Loading…</p>}
            {error && <p className="text-xs text-red-600">{error}</p>}
            {facts && facts.length === 0 && (
              <p className="text-xs text-slate-400">no facts extracted</p>
            )}
            {facts && facts.length > 0 && (
              <ul className="space-y-1">
                {facts.map((f) => (
                  <li
                    key={f.fact_id}
                    className="flex items-center gap-2 text-xs"
                  >
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${
                        f.is_latest ? "bg-emerald-500" : "bg-slate-300"
                      }`}
                    />
                    <span>{f.fact_text}</span>
                    <span className="text-slate-400">({f.predicate})</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    );
  }
  ```

- [ ] **Step 2: Write the real `ui/src/pages/Episodes.tsx`.**
  Loads the episodes envelope for the active namespace and renders the rows;
  pagination via the shared control. Ordering is the endpoint's (`t_ref DESC`).

  ```tsx
  import { useEffect, useState } from "react";
  import { listEpisodes } from "../api";
  import type { Episode } from "../types";
  import Pagination from "../components/Pagination";
  import EpisodeRow from "../components/EpisodeRow";

  const PAGE_SIZE = 50;

  export default function Episodes({ ns }: { ns: string }) {
    const [rows, setRows] = useState<Episode[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
      setPage(1);
    }, [ns]);

    useEffect(() => {
      if (!ns) return;
      let cancelled = false;
      setLoading(true);
      setError(null);
      listEpisodes(ns, page, PAGE_SIZE)
        .then((env) => {
          if (cancelled) return;
          setRows(env.items);
          setTotal(env.total);
        })
        .catch((e) => !cancelled && setError(String(e)))
        .finally(() => !cancelled && setLoading(false));
      return () => {
        cancelled = true;
      };
    }, [ns, page]);

    if (!ns) {
      return <div className="p-6 text-sm text-slate-500">No namespace selected.</div>;
    }

    return (
      <div className="space-y-4 p-6">
        {error && <p className="text-sm text-red-600">{error}</p>}
        {rows.length === 0 && !loading && !error && (
          <p className="text-sm text-slate-400">no episodes in this namespace</p>
        )}
        <div className="space-y-2">
          {rows.map((ep) => (
            <EpisodeRow key={ep.id} ns={ns} episode={ep} />
          ))}
        </div>
        <Pagination
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
          onPage={setPage}
        />
      </div>
    );
  }
  ```

- [ ] **Step 3: Run the gate.**

  ```bash
  cd ui && bun run typecheck && bun run build
  ```

  Expected: PASS.

- [ ] **Step 4: Commit.**

  ```bash
  git add ui/src/pages/Episodes.tsx ui/src/components/EpisodeRow.tsx
  git commit -m "feat(ui): Episodes page — transcript list with per-episode fact expansion"
  ```

---

### Task 14: Activity & Traces page

A polling feed (3-5 s `setInterval` in a `useEffect`) of `/views/{ns}/events`:
add rows expand to their `fact_ids`/`superseded_fact_ids`; an error badge
shows when `payload.error` is present; search rows expand to a per-hit score
table (final = 0.6·relevance + 0.2·recency + 0.2·importance, shown alongside
dense/sparse ranks and RRF). A test-search box POSTs `/views/{ns}/test-search`
and is labeled "runs a real search — updates access stats." When the events
list is empty *and* the namespaces show no `earliest_ts`, a missing-sidecar
hint is shown instead of an empty table.

**Files:**
- Modify: `ui/src/pages/Activity.tsx`
- Create: `ui/src/components/EventFeed.tsx`
- Create: `ui/src/components/ScoreTable.tsx`
- Create: `ui/src/components/TestSearchBox.tsx`
- Test (gate): `bun run typecheck && bun run build` from `ui/`

**Interfaces:**
- Consumes: `GET /views/{ns}/events?kind&page` (envelope of `EventRow`),
  `POST /views/{ns}/test-search {query, k}` (`{hits, duration_ms}`),
  `GET /views/namespaces` (to read the active namespace's
  `activity.earliest_ts` for the missing-sidecar hint).
- Produces: the rendered Activity & Traces page.

- [ ] **Step 1: Write `ui/src/components/ScoreTable.tsx`.**
  Renders the per-hit score decomposition for a search event's hits. The
  weighted-sum formula is computed and shown next to the server's
  `final_score` so a mismatch is visible; dense/sparse ranks and RRF are
  columns.

  ```tsx
  import type { SearchHit } from "../types";

  function weighted(hit: SearchHit): number {
    return 0.6 * hit.relevance + 0.2 * hit.recency + 0.2 * hit.importance;
  }

  export default function ScoreTable({ hits }: { hits: SearchHit[] }) {
    if (hits.length === 0) {
      return <p className="text-xs text-slate-400">no hits</p>;
    }
    return (
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-left text-slate-500">
            <tr>
              <th className="py-1 pr-3">fact</th>
              <th className="py-1 pr-3 text-right">final</th>
              <th className="py-1 pr-3 text-right">0.6·rel+0.2·rec+0.2·imp</th>
              <th className="py-1 pr-3 text-right">rel</th>
              <th className="py-1 pr-3 text-right">rec</th>
              <th className="py-1 pr-3 text-right">imp</th>
              <th className="py-1 pr-3 text-right">dense</th>
              <th className="py-1 pr-3 text-right">sparse</th>
              <th className="py-1 pr-3 text-right">rrf</th>
            </tr>
          </thead>
          <tbody>
            {hits.map((h) => (
              <tr key={h.fact_id} className="border-t border-slate-100">
                <td className="max-w-[16rem] truncate py-1 pr-3">{h.fact_text}</td>
                <td className="py-1 pr-3 text-right tabular-nums">
                  {h.final_score.toFixed(3)}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-500">
                  {weighted(h).toFixed(3)}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums">
                  {h.relevance.toFixed(3)}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums">
                  {h.recency.toFixed(3)}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums">
                  {h.importance.toFixed(3)}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums">
                  {h.dense_rank ?? "—"}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums">
                  {h.sparse_rank ?? "—"}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums">
                  {h.rrf_score === null ? "—" : h.rrf_score.toFixed(3)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  ```

- [ ] **Step 2: Write `ui/src/components/EventFeed.tsx`.**
  Renders the polled event rows. Each row shows kind, timestamp, duration, and
  an error badge from `payload.error`; add rows expand to fact ids created and
  superseded; search rows expand to the `ScoreTable`. Type-narrows the payload
  by `kind`.

  ```tsx
  import { useState } from "react";
  import type { AddPayload, EventRow, SearchPayload } from "../types";
  import { formatTs } from "../lib/format";
  import ScoreTable from "./ScoreTable";

  function Badge({ text, tone }: { text: string; tone: "add" | "search" | "error" }) {
    const cls =
      tone === "error"
        ? "bg-red-100 text-red-700"
        : tone === "add"
          ? "bg-indigo-100 text-indigo-700"
          : "bg-sky-100 text-sky-700";
    return (
      <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>
        {text}
      </span>
    );
  }

  function AddRow({ payload }: { payload: AddPayload }) {
    return (
      <div className="space-y-2 text-xs">
        <div>
          <span className="text-slate-500">created:</span>{" "}
          {payload.fact_ids.length === 0 ? (
            <span className="text-slate-400">none</span>
          ) : (
            <span className="break-all font-mono">
              {payload.fact_ids.join(", ")}
            </span>
          )}
        </div>
        <div>
          <span className="text-slate-500">superseded:</span>{" "}
          {payload.superseded_fact_ids.length === 0 ? (
            <span className="text-slate-400">none</span>
          ) : (
            <span className="break-all font-mono">
              {payload.superseded_fact_ids.join(", ")}
            </span>
          )}
        </div>
        <div className="text-slate-400">
          source {payload.source} · {payload.episode_text_chars} chars ·{" "}
          {payload.fact_count} facts
        </div>
      </div>
    );
  }

  function Row({ event }: { event: EventRow }) {
    const [open, setOpen] = useState(false);
    const errored = Boolean(event.payload.error);
    return (
      <div className="rounded-lg border border-slate-200 bg-white">
        <button
          className="flex w-full items-center gap-3 px-4 py-2 text-left"
          onClick={() => setOpen((o) => !o)}
        >
          <span className="text-slate-400">{open ? "▾" : "▸"}</span>
          <Badge text={event.kind} tone={event.kind} />
          {errored && <Badge text="error" tone="error" />}
          <span className="text-xs text-slate-500">{formatTs(event.ts)}</span>
          <span className="ml-auto text-xs tabular-nums text-slate-400">
            {event.duration_ms.toFixed(0)} ms
          </span>
        </button>
        {open && (
          <div className="border-t border-slate-100 px-4 py-3">
            {errored && (
              <p className="mb-2 text-xs text-red-600">
                {event.payload.error}
              </p>
            )}
            {event.kind === "add" ? (
              <AddRow payload={event.payload as AddPayload} />
            ) : (
              <div className="space-y-2">
                <div className="text-xs text-slate-500">
                  query: “{(event.payload as SearchPayload).query}” · k=
                  {(event.payload as SearchPayload).k} · origin{" "}
                  {(event.payload as SearchPayload).origin}
                </div>
                <ScoreTable hits={(event.payload as SearchPayload).hits} />
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  export default function EventFeed({ events }: { events: EventRow[] }) {
    return (
      <div className="space-y-2">
        {events.map((e) => (
          <Row key={e.id} event={e} />
        ))}
      </div>
    );
  }
  ```

- [ ] **Step 3: Write `ui/src/components/TestSearchBox.tsx`.**
  Posts to `/views/{ns}/test-search`, shows the returned hits in the same
  `ScoreTable`, and carries the required label. On success it calls
  `onRan()` so the parent can refresh the feed (the test-search records an
  `origin:"ui"` event).

  ```tsx
  import { useState } from "react";
  import { testSearch } from "../api";
  import type { SearchHit } from "../types";
  import ScoreTable from "./ScoreTable";

  export default function TestSearchBox({
    ns,
    onRan,
  }: {
    ns: string;
    onRan: () => void;
  }) {
    const [query, setQuery] = useState("");
    const [k, setK] = useState(5);
    const [hits, setHits] = useState<SearchHit[] | null>(null);
    const [duration, setDuration] = useState<number | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [running, setRunning] = useState(false);

    async function run(e: React.FormEvent) {
      e.preventDefault();
      if (!query.trim()) return;
      setRunning(true);
      setError(null);
      try {
        const res = await testSearch(ns, query.trim(), k);
        setHits(res.hits);
        setDuration(res.duration_ms);
        onRan();
      } catch (err) {
        setError(String(err));
      } finally {
        setRunning(false);
      }
    }

    return (
      <div className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
        <div>
          <h2 className="text-sm font-semibold">Test search</h2>
          <p className="text-xs text-amber-600">
            Runs a real search — updates access stats (touch()).
          </p>
        </div>
        <form className="flex flex-wrap items-end gap-3" onSubmit={run}>
          <input
            className="min-w-[16rem] flex-1 rounded border border-slate-300 px-3 py-2 text-sm"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="query"
          />
          <label className="flex flex-col text-xs text-slate-500">
            k
            <input
              type="number"
              min="1"
              max="50"
              className="mt-1 w-16 rounded border border-slate-300 px-2 py-1 text-sm"
              value={k}
              onChange={(e) => setK(Math.max(1, Number(e.target.value)))}
            />
          </label>
          <button
            type="submit"
            disabled={running || query.trim() === ""}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-40"
          >
            {running ? "Searching…" : "Search"}
          </button>
        </form>
        {error && <p className="text-xs text-red-600">{error}</p>}
        {hits && (
          <div>
            <div className="mb-1 text-xs text-slate-400">
              {hits.length} hits · {duration?.toFixed(0)} ms
            </div>
            <ScoreTable hits={hits} />
          </div>
        )}
      </div>
    );
  }
  ```

- [ ] **Step 4: Write the real `ui/src/pages/Activity.tsx`.**
  Polls the events endpoint every 4 s, filters by kind, shows the test-search
  box, and renders the missing-sidecar hint when the feed is empty *and* the
  active namespace reports no `earliest_ts` (no sidecar has recorded anything).
  The poll is paused when the tab is hidden to avoid needless load.

  ```tsx
  import { useCallback, useEffect, useRef, useState } from "react";
  import { listEvents, listNamespaces } from "../api";
  import type { EventRow } from "../types";
  import EventFeed from "../components/EventFeed";
  import TestSearchBox from "../components/TestSearchBox";

  const POLL_MS = 4000;
  const PAGE_SIZE = 50;

  type KindFilter = "all" | "add" | "search";

  export default function Activity({ ns }: { ns: string }) {
    const [events, setEvents] = useState<EventRow[]>([]);
    const [kind, setKind] = useState<KindFilter>("all");
    const [error, setError] = useState<string | null>(null);
    const [hasSidecar, setHasSidecar] = useState<boolean | null>(null);
    const loadedOnce = useRef(false);

    const fetchEvents = useCallback(() => {
      if (!ns) return;
      const k = kind === "all" ? undefined : kind;
      listEvents(ns, k, 1, PAGE_SIZE)
        .then((env) => {
          setEvents(env.items);
          setError(null);
          loadedOnce.current = true;
        })
        .catch((e) => setError(String(e)));
    }, [ns, kind]);

    // read earliest_ts for the active namespace to decide the sidecar hint
    useEffect(() => {
      if (!ns) return;
      let cancelled = false;
      listNamespaces()
        .then((all) => {
          if (cancelled) return;
          const card = all.find((c) => c.name === ns);
          setHasSidecar(card ? card.activity.earliest_ts !== null : null);
        })
        .catch(() => setHasSidecar(null));
      return () => {
        cancelled = true;
      };
    }, [ns, events.length]);

    useEffect(() => {
      loadedOnce.current = false;
      fetchEvents();
      const id = window.setInterval(() => {
        if (document.hidden) return;
        fetchEvents();
      }, POLL_MS);
      return () => window.clearInterval(id);
    }, [fetchEvents]);

    if (!ns) {
      return <div className="p-6 text-sm text-slate-500">No namespace selected.</div>;
    }

    const missingSidecar =
      loadedOnce.current && events.length === 0 && hasSidecar === false;

    return (
      <div className="space-y-4 p-6">
        <TestSearchBox ns={ns} onRan={fetchEvents} />

        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-500">filter</span>
          {(["all", "add", "search"] as KindFilter[]).map((k) => (
            <button
              key={k}
              className={`rounded px-2 py-1 text-xs ${
                kind === k
                  ? "bg-slate-900 text-white"
                  : "border border-slate-300 text-slate-600"
              }`}
              onClick={() => setKind(k)}
            >
              {k}
            </button>
          ))}
          <span className="ml-auto text-[11px] text-slate-400">
            polling every {POLL_MS / 1000}s
          </span>
        </div>

        {error && <p className="text-sm text-red-600">{error}</p>}

        {missingSidecar ? (
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
            No event traces for this namespace. Connect via the observing MCP
            (<code>uvx lean-memory-console mcp</code>) so adds and searches are
            captured — the core stdio server writes memories but no{" "}
            <code>_events.db</code> sidecar.
          </div>
        ) : (
          <EventFeed events={events} />
        )}

        {!missingSidecar && events.length === 0 && !error && (
          <p className="text-sm text-slate-400">no events yet</p>
        )}
      </div>
    );
  }
  ```

- [ ] **Step 5: Run the gate.**

  ```bash
  cd ui && bun run typecheck && bun run build
  ```

  Expected: PASS.

- [ ] **Step 6: Commit.**

  ```bash
  git add ui/src/pages/Activity.tsx ui/src/components/EventFeed.tsx \
    ui/src/components/ScoreTable.tsx ui/src/components/TestSearchBox.tsx
  git commit -m "feat(ui): Activity & Traces page — polled feed, score decomposition, test search"
  ```
## Distribution + E2E (Tasks 15–17)

These tasks package the console for the three distribution channels (Docker,
Claude Code plugin, PyPI) and pin the manual end-to-end verification. They
consume the finished console package (`cli.py` with `serve`/`mcp`/`--print-compose-path`,
`observe_mcp.py`, `app.py`) from Tasks 1–14 and the built SPA under
`console/src/lean_memory_console/static/`. They produce no new Python modules —
only deploy artifacts, plugin manifests/commands, and documentation. Per the
lane-D rule, **no commit in these tasks touches `src/lean_memory/`, `bench/`,
`tests/`, or the root `pyproject.toml`.**

Reference facts baked in from source (verified during planning):

- Core stdio MCP server exposes exactly `memory_add(namespace, text)`,
  `memory_search(namespace, query, k=5)`, `memory_clear(namespace)`
  (`src/lean_memory/mcp_server.py`). The observing wrapper is the deliberate
  superset asserted by the §12 parity test.
- `Memory.__init__` default root is `./lm_data` (`src/lean_memory/memory.py:44`)
  — the `./lm_data` mismatch trap the docs and `/memory:status` must warn about.
- `Memory.search(..., is_latest_only=True)` (`memory.py:180`); the wire name is
  `latest_only`.

---

### Task 15: Docker distribution — multi-stage Dockerfile + compose

**Files:**
- Create: `deploy/Dockerfile`
- Verify: `deploy/docker-compose.yml` (created with final content in Task 1;
  Step 4 restates it byte-identically so this task stands alone)
- Create: `console/tests/test_deploy_artifacts.py`
- Modify: `console/pyproject.toml` (repeat the hatch force-include snippet so
  this task stands alone; already wired in Task 1/9 — the Edit is idempotent
  and a no-op if the block is already present)

**Interfaces:**
- Consumes: the console package installable from the repo checkout
  (`console/` with core `lean-memory` as a path/PyPI dependency); the built SPA
  under `console/src/lean_memory_console/static/`; `lean-memory[models]` extra
  (real embedder + reranker) for the `full` target.
- Produces: `deploy/Dockerfile` (targets `slim`, `full`), `deploy/docker-compose.yml`
  (service `console`, `build.target: full`), and the packaged copy of the
  compose file inside the wheel at `lean_memory_console/deploy/docker-compose.yml`
  (via hatch `force-include`) that `cli.py --print-compose-path` resolves.

- [ ] **Step 1: Write the deploy-artifact static tests (failing).**
  These tests read the files as text and assert structural invariants — no
  Docker daemon required, so they run in CI. Write the full file:

  ```python
  # console/tests/test_deploy_artifacts.py
  """Static structural checks on the Docker deploy artifacts.

  No Docker daemon is invoked — these assert file contents so the invariants
  (multi-stage targets, full-is-default, required-env fail-fast, packaged
  compose path) hold in CI. The `docker build` smoke check is a [manual] step
  in the task, not a test.
  """
  from __future__ import annotations

  from pathlib import Path

  import yaml  # PyYAML — in the console[dev] extra since Task 1

  REPO_ROOT = Path(__file__).resolve().parents[2]
  DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"
  COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"


  def test_dockerfile_has_three_named_stages() -> None:
      text = DOCKERFILE.read_text()
      # bun build stage, then slim, then full (order matters: full FROM slim).
      assert "oven/bun" in text, "bun stage must use the oven/bun image"
      assert "AS ui-build" in text
      assert "python:3.13-slim AS slim" in text
      assert "FROM slim AS full" in text
      # full adds the models extra; slim must NOT.
      slim_block = text.split("FROM slim AS full")[0]
      assert "[models]" not in slim_block, "slim target must never install [models]"
      assert "[models]" in text, "full target must install lean-memory[models]"


  def test_dockerfile_copies_built_static_from_bun_stage() -> None:
      text = DOCKERFILE.read_text()
      assert "COPY --from=ui-build" in text, "built SPA must come from the bun stage"
      assert "static" in text


  def test_compose_targets_full_and_requires_api_key() -> None:
      data = yaml.safe_load(COMPOSE.read_text())
      svc = data["services"]["console"]
      assert svc["build"]["target"] == "full", "compose must default to the full image"
      assert svc["build"]["context"] == ".."
      assert svc["build"]["dockerfile"] == "deploy/Dockerfile"
      assert "8377:8377" in svc["ports"]
      # LM_API_KEY required — the ${VAR:?message} form fails compose if unset.
      raw = COMPOSE.read_text()
      assert "${LM_API_KEY:?" in raw, "LM_API_KEY must be a required compose variable"
      assert "LM_DATA_ROOT=/data" in raw
      # named data volume + hf cache mount.
      assert "lm_data:/data" in raw
      assert "huggingface" in raw


  def test_compose_path_resolves_and_matches_source() -> None:
      # cli._compose_path() prefers the wheel-packaged resource and falls back
      # to the repo deploy/ copy under editable installs (Task 9). Either way
      # the resolved file must exist and be byte-identical to the source of
      # truth. (Wheel packaging itself is validated by Step 5's `unzip -l`
      # listing — importlib.resources cannot see force-include under an
      # editable install, so asserting the raw resource here would fail in dev.)
      from lean_memory_console.cli import _compose_path

      resolved = _compose_path()
      assert resolved.is_file()
      assert resolved.read_text() == COMPOSE.read_text()
  ```

- [ ] **Step 2: Run the tests — Expected: FAIL.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_deploy_artifacts.py -v
  ```
  Expected: FAIL — the two Dockerfile tests raise `FileNotFoundError`
  (`deploy/Dockerfile` does not exist yet). The compose tests may already pass:
  `deploy/docker-compose.yml` was created in Task 1 and `_compose_path` in
  Task 9.

- [ ] **Step 3: Write `deploy/Dockerfile` (full contents).**

  ```dockerfile
  # syntax=docker/dockerfile:1
  #
  # lean-memory-console — multi-stage image.
  #
  #   ui-build : oven/bun — builds the React SPA into dist/.
  #   slim     : python:3.13-slim — console + core lean-memory + built SPA,
  #              stub embedder only (never installs [models]). For API/UI dev.
  #   full     : slim + lean-memory[models] (CPU torch, real embedder+reranker).
  #              This is the documented first-run image (compose target: full).
  #
  # Build the default (full) image:
  #   docker build -f deploy/Dockerfile --target full -t lean-memory-console .
  # Build the slim dev image:
  #   docker build -f deploy/Dockerfile --target slim -t lean-memory-console:slim .

  # ── UI build stage ───────────────────────────────────────────────────────
  FROM oven/bun:1 AS ui-build
  WORKDIR /ui
  # Install deps first for layer caching.
  COPY ui/package.json ui/bun.lock ./
  RUN bun install --frozen-lockfile
  COPY ui/ ./
  RUN bun run build
  # Vite emits the SPA to /ui/dist.

  # ── slim runtime (stub embedder; no models) ──────────────────────────────
  FROM python:3.13-slim AS slim
  ENV PYTHONUNBUFFERED=1 \
      PIP_NO_CACHE_DIR=1 \
      PIP_DISABLE_PIP_VERSION_CHECK=1 \
      LM_DATA_ROOT=/data \
      PORT=8377
  WORKDIR /app
  # Core engine + console sources from the repo checkout (build context = repo root).
  COPY pyproject.toml README.md ./
  COPY src/ ./src/
  COPY console/ ./console/
  COPY deploy/ ./deploy/
  # Install the core engine (path) then the console (path). No [models] here.
  RUN python -m pip install --upgrade pip \
   && python -m pip install . \
   && python -m pip install ./console
  # Drop the built SPA into the installed package's static dir.
  COPY --from=ui-build /ui/dist/ /app/console/src/lean_memory_console/static/
  RUN python -m pip install --no-deps --force-reinstall ./console
  VOLUME ["/data"]
  EXPOSE 8377
  # Docker mode: the app serves the console UI + data plane; LM_API_KEY required
  # (boot validation in cli.py refuses to start without it).
  CMD ["lean-memory-console", "serve", "--root", "/data", "--no-open"]

  # ── full runtime (real models) ───────────────────────────────────────────
  FROM slim AS full
  # CPU torch + sentence-transformers + reranker via the core [models] extra.
  RUN python -m pip install '.[models]'
  # HF cache lives on a mounted volume so downloads survive container restarts.
  ENV HF_HOME=/root/.cache/huggingface
  VOLUME ["/root/.cache/huggingface"]
  ```

  **Design notes (bake into the code, not left as prose in the image):**
  - `--no-open` is passed because there is no browser in the container; the
    human connects from the host. Boot validation (`cli.py`, spec §10) still
    enforces `LM_API_KEY` in Docker mode — the container refuses to boot
    without it, surfaced by compose's `${LM_API_KEY:?…}` guard *and* the
    in-process check.
  - The double console install (`pip install ./console` then, after the SPA
    copy, `--no-deps --force-reinstall ./console`) reinstalls the package so
    the copied `static/` assets are captured in the installed tree without
    re-resolving dependencies.
  - `sqlite-vec` is a hard dependency of the core engine (installed by
    `pip install .`) and is boot-checked; no extra step needed.

- [ ] **Step 4: Verify `deploy/docker-compose.yml` matches the authoritative contents.**
  This file is the **single source of truth** referenced by
  `lean-memory-console --print-compose-path` and by the `/memory:server-up|down`
  plugin commands (Task 16). The plugin does **not** ship its own copy. It was
  created in Task 1 Step 1; if the file on disk differs from the block below,
  update it to match — this block is authoritative and byte-identical to
  Task 1's:

  ```yaml
  # deploy/docker-compose.yml — single-tenant lean-memory-console (Docker mode).
  #
  # Usage:
  #   LM_API_KEY=$(openssl rand -hex 24) docker compose -f deploy/docker-compose.yml up -d
  #
  # Single source of truth (spec §9): the console CLI packages this exact file
  # into the wheel; the plugin's /memory:server-up|down commands resolve it via
  #   docker compose -f "$(lean-memory-console --print-compose-path)" up -d
  services:
    console:
      build:
        context: ..
        dockerfile: deploy/Dockerfile
        target: full            # full is the default first-run image (real models)
      ports:
        - "8377:8377"
      environment:
        # LM_API_KEY is required in Docker mode — compose refuses to start
        # without it (the console also boot-checks it).
        - LM_API_KEY=${LM_API_KEY:?set LM_API_KEY - e.g. openssl rand -hex 24}
        - LM_DATA_ROOT=/data
        - PORT=8377
        - LM_CONSOLE_MODELS=auto
      volumes:
        - lm_data:/data
        - hf_cache:/root/.cache/huggingface
      restart: unless-stopped

  volumes:
    lm_data:
    hf_cache:
  ```

- [ ] **Step 5: Re-assert the hatch force-include in `console/pyproject.toml`.**
  This wiring already lands in Task 1/9; the snippet is repeated here so Task 15
  stands alone. Edit `console/pyproject.toml` to ensure the wheel build force-includes
  the repo compose file at the packaged path (idempotent — skip the Edit if the
  block already matches). The authoritative block:

  ```toml
  [tool.hatch.build.targets.wheel.force-include]
  # Package the single-source-of-truth compose file into the wheel so
  # `lean-memory-console --print-compose-path` and the plugin commands resolve
  # it from the installed package, not a plugin-bundled copy.
  "../deploy/docker-compose.yml" = "lean_memory_console/deploy/docker-compose.yml"
  ```

  If `console/pyproject.toml` uses a package-relative build (the console package
  lives under `console/src/lean_memory_console/`), the force-include source path
  is relative to the `console/` directory that holds `pyproject.toml`; `../deploy/…`
  reaches the repo-root `deploy/`. Verify the built wheel actually contains the
  resource with:
  ```bash
  console/.venv/bin/python -m pip install -e ./console && \
  console/.venv/bin/python -c "import importlib.resources as r; print(r.files('lean_memory_console').joinpath('deploy/docker-compose.yml').read_text()[:60])"
  ```
  Note: editable installs may not exercise `force-include`; the packaged-resource
  test (Step 1, `test_compose_is_packaged_into_the_wheel`) is the authoritative
  check. If editable install does not surface the resource, run a real wheel
  build to validate:
  ```bash
  console/.venv/bin/python -m pip install build && \
  console/.venv/bin/python -m build --wheel ./console && \
  unzip -l console/dist/*.whl | grep 'deploy/docker-compose.yml'
  ```
  Expected: the wheel listing shows `lean_memory_console/deploy/docker-compose.yml`.

- [ ] **Step 6: Run the deploy-artifact tests — Expected: PASS.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_deploy_artifacts.py -v
  ```
  Expected: PASS (4 tests) under the plain editable install — no wheel build
  needed; the wheel packaging itself is validated by Step 5's `unzip -l`
  listing.

- [ ] **Step 7: [manual] Dockerfile lint + slim build smoke.**
  Not run in CI (requires the Docker daemon / network). Perform once locally
  before merge:
  - Hadolint-style manual checklist (walk the Dockerfile against each):
    - [ ] Every `FROM` pins a concrete tag (`oven/bun:1`, `python:3.13-slim`), no `latest`.
    - [ ] `pip` runs with `--no-cache-dir` (set via `PIP_NO_CACHE_DIR=1`).
    - [ ] No secrets or `LM_API_KEY` baked into any layer (it arrives at runtime).
    - [ ] `slim` block contains no `[models]` install.
    - [ ] Built SPA is copied `--from=ui-build` into the installed static dir.
    - [ ] `VOLUME`s declared for `/data` and the HF cache.
  - Slim build smoke (fast — no torch):
    ```bash
    docker build -f deploy/Dockerfile --target slim -t lean-memory-console:slim .
    ```
    Expected: image builds; `docker run --rm -e LM_API_KEY=x -e LM_CONSOLE_MODELS=stub \
    lean-memory-console:slim lean-memory-console --print-compose-path` prints a
    path ending `lean_memory_console/deploy/docker-compose.yml`.
  - Optional full build (slow — CPU torch wheels are heavy; documented image-size
    risk, spec §15): `docker build -f deploy/Dockerfile --target full -t lean-memory-console .`

- [ ] **Step 8: Commit.**
  ```bash
  git add deploy/Dockerfile deploy/docker-compose.yml \
          console/tests/test_deploy_artifacts.py console/pyproject.toml
  git commit -m "feat(console): multi-stage Docker image (slim/full) + compose, packaged into wheel"
  ```

---

### Task 16: Claude Code plugin — manifest, MCP entry, commands, marketplace

**Files:**
- Create: `plugin/.claude-plugin/plugin.json`
- Create: `plugin/.mcp.json`
- Create: `plugin/commands/memory-ui.md`
- Create: `plugin/commands/memory-status.md`
- Create: `plugin/commands/memory-server-up.md`
- Create: `plugin/commands/memory-server-down.md`
- Create: `.claude-plugin/marketplace.json` (repo root)
- Create: `console/tests/test_plugin_manifest.py`

**Interfaces:**
- Consumes: the `lean-memory-console` console script (`serve`, `mcp`,
  `--print-compose-path`) from `cli.py`; the packaged `deploy/docker-compose.yml`.
- Produces: an installable Claude Code plugin (`plugin/`) surfacing the
  observing MCP over stdio and four slash commands; a repo-root marketplace
  manifest making the repo its own plugin marketplace (spec §9).

- [ ] **Step 1: Write the plugin-manifest tests (failing).**
  Validate JSON shapes and command frontmatter without any Claude CLI. Full file:

  ```python
  # console/tests/test_plugin_manifest.py
  """Structural checks on the Claude Code plugin manifests and command files.

  These assert the pinned contract from spec §9: the .mcp.json carries ONLY the
  stdio uvx entry; the marketplace points the `lean-memory` plugin at ./plugin;
  the four commands exist with the right slash names and shell out to the
  packaged compose file (not a bundled copy).
  """
  from __future__ import annotations

  import json
  from pathlib import Path

  REPO_ROOT = Path(__file__).resolve().parents[2]
  PLUGIN = REPO_ROOT / "plugin"
  MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"


  def test_plugin_json_identity() -> None:
      data = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
      assert data["name"] == "lean-memory"
      assert data["version"] == "0.1.0"
      assert data["description"]  # non-empty


  def test_mcp_json_has_only_the_stdio_entry() -> None:
      data = json.loads((PLUGIN / ".mcp.json").read_text())
      servers = data["mcpServers"]
      # Exactly one server, the stdio observing wrapper — no http entry (spec §9).
      assert list(servers.keys()) == ["lean-memory"]
      entry = servers["lean-memory"]
      assert entry["command"] == "uvx"
      assert entry["args"] == ["lean-memory-console", "mcp"]
      assert "url" not in entry and "transport" not in entry


  def test_marketplace_points_at_local_plugin() -> None:
      data = json.loads(MARKETPLACE.read_text())
      assert data["name"] == "lean-memory-console"
      assert "owner" in data and isinstance(data["owner"], dict)
      plugins = data["plugins"]
      assert len(plugins) == 1
      p = plugins[0]
      assert p["name"] == "lean-memory"
      assert p["source"] == "./plugin"
      assert p["description"]


  def test_all_four_commands_exist_with_frontmatter() -> None:
      cmds = {
          "memory-ui.md": "/memory:ui",
          "memory-status.md": "/memory:status",
          "memory-server-up.md": "/memory:server-up",
          "memory-server-down.md": "/memory:server-down",
      }
      for filename in cmds:
          path = PLUGIN / "commands" / filename
          assert path.is_file(), f"missing command file {filename}"
          text = path.read_text()
          # YAML frontmatter block.
          assert text.startswith("---\n"), f"{filename} must open with frontmatter"
          assert "description:" in text.split("---", 2)[1]


  def test_server_commands_use_packaged_compose_path() -> None:
      up = (PLUGIN / "commands" / "memory-server-up.md").read_text()
      down = (PLUGIN / "commands" / "memory-server-down.md").read_text()
      # Both resolve the compose file via the console CLI, never a bundled copy.
      assert 'lean-memory-console --print-compose-path' in up
      assert "up -d" in up
      assert 'lean-memory-console --print-compose-path' in down
      assert "down" in down
      # No plugin-bundled compose file.
      assert not (PLUGIN / "docker-compose.yml").exists()


  def test_status_command_warns_about_lm_data_trap() -> None:
      text = (PLUGIN / "commands" / "memory-status.md").read_text()
      assert "./lm_data" in text, "status must warn about the ./lm_data mismatch trap"
      assert "lean-memory-console" in text
  ```

- [ ] **Step 2: Run the tests — Expected: FAIL.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_plugin_manifest.py -v
  ```
  Expected: FAIL with `FileNotFoundError` — none of the plugin files exist yet.

- [ ] **Step 3: Write `plugin/.claude-plugin/plugin.json`.**
  ```json
  {
    "name": "lean-memory",
    "version": "0.1.0",
    "description": "Local-first agent memory for Claude Code: an observing MCP that stores and searches memories, plus a read-only console to verify the supersession spine, score breakdown, and provenance episodes."
  }
  ```

- [ ] **Step 4: Write `plugin/.mcp.json` (stdio entry ONLY).**
  The HTTP/Docker connection is intentionally absent — a second auto-enabled
  entry would hard-fail config parse when `LM_API_KEY` is unset and otherwise
  spawn a dead connection (spec §9). Docker users run the one-line
  `claude mcp add --transport http …` snippet the console displays.
  ```json
  {
    "mcpServers": {
      "lean-memory": {
        "command": "uvx",
        "args": ["lean-memory-console", "mcp"]
      }
    }
  }
  ```

- [ ] **Step 5: Write `plugin/commands/memory-ui.md` (`/memory:ui`).**
  ```markdown
  ---
  description: Launch the local lean-memory console and open it in the browser.
  ---

  Start the transient read-only console over the resolved data root and open the
  tokened URL. The server binds `127.0.0.1`, prints a URL containing a
  single-use session token, and runs until you press Ctrl-C.

  Run the console in the background and surface its URL:

  !`lean-memory-console serve`

  Notes for the user:
  - The console is **read-only** over stored memory content. The only write it
    performs is the manual test-search box, which runs a real engine search and
    therefore bumps access stats (this is observability of live search, not a
    memory mutation).
  - It serves exactly one data root: `--root` > `LM_DATA_ROOT` > `~/.lean_memory`.
    If your agent wrote to the engine's own default `./lm_data`, pass
    `--root ./lm_data` so you inspect the right root (see `/memory:status`).
  - The session token dies with the process. Closing the terminal (Ctrl-C) stops
    the server.

  If a browser did not open automatically, open the printed
  `http://127.0.0.1:8377/?token=…` URL manually.
  ```

- [ ] **Step 6: Write `plugin/commands/memory-status.md` (`/memory:status`).**
  ```markdown
  ---
  description: Show the resolved data root, its namespaces, and connect snippets — with the ./lm_data mismatch warning.
  ---

  Report where memory actually lives and how to connect, so the human never
  inspects an empty root while the agent wrote elsewhere.

  1. Print the resolved data root (the console applies `--root` >
     `LM_DATA_ROOT` > `~/.lean_memory`) and list its namespaces:

     !`lean-memory-console serve --root "${LM_DATA_ROOT:-$HOME/.lean_memory}" --print-status 2>/dev/null || lean-memory-console mcp --help >/dev/null 2>&1; echo "Resolved root: ${LM_DATA_ROOT:-$HOME/.lean_memory}"`

     Then enumerate namespace `.db` files under that root (skipping `_*.db`):

     !`ls -1 "${LM_DATA_ROOT:-$HOME/.lean_memory}"/*.db 2>/dev/null | grep -v '/_' || echo "(no namespaces yet)"`

  2. **./lm_data mismatch warning.** The core engine's *own* default root is
     `./lm_data`, not `~/.lean_memory`. If `./lm_data` exists in the current
     project but is not the served root, the human would silently inspect an
     empty root. Warn when it exists:

     !`test -d ./lm_data && echo "WARNING: ./lm_data exists here. Your agent may have written memories to ./lm_data (the engine's default root), not ${LM_DATA_ROOT:-$HOME/.lean_memory}. Run '/memory:ui' with --root ./lm_data to inspect it." || echo "No ./lm_data in this directory."`

  3. **Connect snippets.**
     - Local observing MCP (this plugin already wires it via `.mcp.json`):
       `uvx lean-memory-console mcp`
     - Open the console: `/memory:ui`
     - Docker HTTP (after `/memory:server-up`):
       ```
       claude mcp add --transport http lean-memory http://127.0.0.1:8377/mcp \
         --header "Authorization: Bearer $LM_API_KEY"
       ```

  Guidance: use **one namespace per project/session** — cross-process writers on
  a single namespace serialize via retry, not a lock manager.
  ```

- [ ] **Step 7: Write `plugin/commands/memory-server-up.md` (`/memory:server-up`).**
  Resolves the compose file via the console CLI — no plugin-bundled copy
  (`deploy/docker-compose.yml` in the repo/wheel is the single source of truth).
  ```markdown
  ---
  description: Start the single-tenant lean-memory console in Docker (full image, real models).
  ---

  Bring up the Docker console using the compose file packaged inside the
  installed `lean-memory-console` wheel (the single source of truth — the plugin
  ships no compose copy).

  `LM_API_KEY` is **required** in Docker mode; the container refuses to boot
  without it. Set one if you have not already (e.g. `export LM_API_KEY=$(openssl rand -hex 24)`).

  !`docker compose -f "$(lean-memory-console --print-compose-path)" up -d`

  After it starts, connect an agent over streamable-HTTP MCP:

  ```
  claude mcp add --transport http lean-memory http://127.0.0.1:8377/mcp \
    --header "Authorization: Bearer $LM_API_KEY"
  ```

  Open the console UI at `http://127.0.0.1:8377/` and authenticate with the same
  `LM_API_KEY`. Stop it with `/memory:server-down`.
  ```

- [ ] **Step 8: Write `plugin/commands/memory-server-down.md` (`/memory:server-down`).**
  ```markdown
  ---
  description: Stop the Docker lean-memory console (data volume is preserved).
  ---

  Tear down the Docker console using the same packaged compose file. The named
  `lm_data` volume is **not** removed, so your memories persist across restarts.

  !`docker compose -f "$(lean-memory-console --print-compose-path)" down`

  To also delete stored memories and the HF cache (irreversible), add `-v`
  manually:
  `docker compose -f "$(lean-memory-console --print-compose-path)" down -v`
  ```

- [ ] **Step 9: Write `.claude-plugin/marketplace.json` (repo root).**
  On extraction the repo doubles as its own marketplace; the marketplace root is
  the directory containing `.claude-plugin/` (repo root), and the plugin source
  is the sibling `./plugin` directory (spec §9).
  ```json
  {
    "name": "lean-memory-console",
    "owner": {
      "name": "lean-memory",
      "url": "https://github.com/Wuesteon/lean-memory-console"
    },
    "plugins": [
      {
        "name": "lean-memory",
        "source": "./plugin",
        "description": "Observing MCP + read-only console for lean-memory: store and search agent memories from Claude Code, then verify the supersession spine, per-hit score breakdown, and provenance episodes in a local UI."
      }
    ]
  }
  ```

- [ ] **Step 10: Run the tests — Expected: PASS.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_plugin_manifest.py -v
  ```
  Expected: PASS (6 tests).

- [ ] **Step 11: [manual if CLI unavailable] Validate the plugin.**
  Run the Claude Code plugin validator against the marketplace root:
  ```bash
  claude plugin validate .
  ```
  Expected: reports the `lean-memory` plugin as valid (manifest, `.mcp.json`,
  and four commands discovered). Fallback if the standalone CLI subcommand is
  unavailable: inside a Claude Code session run `/plugin validate` after
  `/plugin marketplace add ./` (or `/plugin marketplace add <owner>/lean-memory-console`
  once the repo is published). Mark this step done once either validator passes;
  if no validator is available in the environment, the Step 10 structural tests
  are the CI gate.

- [ ] **Step 12: Commit.**
  ```bash
  git add plugin/.claude-plugin/plugin.json plugin/.mcp.json \
          plugin/commands/memory-ui.md plugin/commands/memory-status.md \
          plugin/commands/memory-server-up.md plugin/commands/memory-server-down.md \
          .claude-plugin/marketplace.json console/tests/test_plugin_manifest.py
  git commit -m "feat(plugin): Claude Code plugin (stdio MCP + 4 commands) and repo marketplace manifest"
  ```

---

### Task 17: Console README + manual E2E verification + whole-suite gate

**Files:**
- Create: `console/README.md`
- Create: `console/tests/test_readme_contract.py`

**Interfaces:**
- Consumes: everything above — both deployment modes (`serve`/`mcp`, Docker),
  the plugin install flow, the `t_ref` live-vs-replay semantics
  (`memory.py`), the one-namespace-per-project guidance (spec §6/§15), the
  `./lm_data` trap (spec §10), and the image-size note (spec §15).
- Produces: the user-facing `console/README.md` (quickstart both modes), the
  verbatim §12 manual E2E checklist, and the final whole-suite green gate that
  proves lane-D was never touched.

- [ ] **Step 1: Write a small README-contract test (failing).**
  The README carries load-bearing operational facts (the `./lm_data` trap, the
  `t_ref` replay rule, the required `LM_API_KEY` in Docker, one-namespace
  guidance). Assert they are present so the doc cannot silently drift. Full file:

  ```python
  # console/tests/test_readme_contract.py
  """The README must document the operational traps the spec pins (§6, §10, §15).

  These are cheap presence checks — they keep the doc honest, not exhaustive.
  """
  from __future__ import annotations

  from pathlib import Path

  README = Path(__file__).resolve().parents[1] / "README.md"


  def test_readme_covers_the_load_bearing_facts() -> None:
      text = README.read_text()
      required = [
          "./lm_data",                         # the data-root mismatch trap (§10)
          "LM_DATA_ROOT",                      # data-root resolution
          "~/.lean_memory",                    # console default root
          "t_ref",                             # live-vs-replay temporal anchor
          "LM_API_KEY",                        # required in Docker mode
          "one namespace per project",         # concurrency guidance (§6/§15)
          "read-only",                         # the console's core claim (§1)
          "torch",                             # image-size note (§15)
      ]
      missing = [s for s in required if s not in text]
      assert not missing, f"README missing required topics: {missing}"


  def test_readme_documents_both_modes() -> None:
      text = README.read_text().lower()
      assert "local" in text and "docker" in text
      assert "serve" in text and "mcp" in text
  ```

- [ ] **Step 2: Run the test — Expected: FAIL.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_readme_contract.py -v
  ```
  Expected: FAIL with `FileNotFoundError` — `console/README.md` does not exist.

- [ ] **Step 3: Write `console/README.md` (full contents).**

  ````markdown
  # lean-memory-console

  A read-only verification console for [lean-memory](https://github.com/Wuesteon/lean-memory).

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

  Via the Claude Code plugin (recommended), the MCP is wired automatically:

  ```
  /plugin marketplace add Wuesteon/lean-memory-console
  /plugin install lean-memory
  /memory:ui        # launch + open the console
  /memory:status    # resolved root, namespaces, connect snippets
  ```

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

  - The console and `/memory:status` **print the resolved root** and **warn**
    when `./lm_data` exists but is not the served root.
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
    pass the original event time as `t_ref` on every add. **Omitting `t_ref` on
    historical data silently collapses the spine's ordering** — every backfilled
    fact gets `now`, so supersession and point-in-time queries become meaningless.

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
  Namespaces whose sanitized name is empty or starts with `_` are **rejected**
  (the `_events.db` sidecar is reserved).

  ## Image size (Docker `full`)

  The default Docker image is the `full` target: it installs `lean-memory[models]`
  (CPU **torch** + sentence-transformers + a cross-encoder reranker), so the
  image is large and the first run downloads model weights into the mounted HF
  cache volume. This is deliberate — stub vectors would recreate the
  FakeEmbedder first-impression failure the quality gate exists to fix. A `slim`
  target (stub embedder, no models) exists for API/UI development
  (`docker build --target slim …`) but is never the documented first-run path.

  ## Offline by default

  The console runs fully on deterministic stub backends with no network and no
  model downloads (`LM_CONSOLE_MODELS=stub`, or `auto` when `[models]` is not
  importable). When scores are stub-generated the UI shows a banner saying so.

  ## License

  Apache-2.0
  ````

- [ ] **Step 4: Run the README-contract test — Expected: PASS.**
  ```bash
  console/.venv/bin/python -m pytest console/tests/test_readme_contract.py -v
  ```
  Expected: PASS (2 tests).

- [ ] **Step 5: Record the manual E2E verification checklist (spec §12).**
  Perform this **before merge** (superpowers `verify` flow). It is a manual gate,
  not automated — capture the observed outcome next to each step. Add the
  checklist verbatim to the bottom of `console/README.md` under a
  `## Manual E2E verification (pre-merge)` heading so it travels with the package:

  **Local mode (plugin + observing MCP):**
  - [ ] `/plugin marketplace add ./` (from the repo root marketplace) →
        `/plugin install lean-memory`.
        Expected: plugin installs; `lean-memory` MCP server appears in the
        session's MCP list.
  - [ ] In a real Claude Code session, ask the agent to store a couple of facts
        and then search them (through the observing MCP `memory_add`/`memory_search`).
        Expected: `memory_add` returns `{fact_ids, superseded_count}`;
        `memory_search` returns hits whose `fact_text` matches what was stored;
        rows land in `<root>/_events.db`.
  - [ ] Run `/memory:ui`.
        Expected: browser opens `http://127.0.0.1:8377/?token=…`; the address bar
        loses `?token` after boot; no login screen (local mode).
  - [ ] Verify all four pages render the resulting state:
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
  - [ ] `export LM_API_KEY=$(openssl rand -hex 24)` then
        `docker compose -f "$(lean-memory-console --print-compose-path)" up -d`
        (or `/memory:server-up`).
        Expected: container starts; boot validation passes (data root writable,
        `LM_API_KEY` set, sqlite-vec loadable).
  - [ ] `claude mcp add --transport http lean-memory http://127.0.0.1:8377/mcp \
          --header "Authorization: Bearer $LM_API_KEY"`.
        Expected: the MCP connection registers; a store+search from the agent
        succeeds and records events in the container's `/data/_events.db`.
  - [ ] Open `http://127.0.0.1:8377/`.
        Expected: the login screen prompts for the key; entering `LM_API_KEY`
        authenticates; all four pages render the same as local mode, and the
        Overview shows the Docker connect snippet.
  - [ ] `/memory:server-down`.
        Expected: container stops; the `lm_data` volume persists (re-`up` shows
        the same memories).

- [ ] **Step 6: Final whole-suite gate — both suites green.**
  The console suite proves the console works end to end; the **core** suite
  proves lane-D was never touched (no changes leaked into `src/lean_memory/`,
  `bench/`, `tests/`, or the root `pyproject.toml`).
  ```bash
  # Console suite (from repo root).
  console/.venv/bin/python -m pytest console/tests -q
  # Core suite — MUST be identically green vs. the branch base (lane-D untouched).
  .venv/bin/python -m pytest tests/ -q
  ```
  Expected: both report all-green with no errors. Confirm the core count matches
  the pre-packet baseline (nothing added/removed in `tests/`). If either fails,
  stop — do not commit — and reconcile before proceeding.

  Also confirm lane-D discipline over the whole packet's diff:
  ```bash
  git diff --name-only $(git merge-base HEAD main)..HEAD -- \
    src/lean_memory bench tests pyproject.toml
  ```
  Expected: **empty output** — no packet commit touched a lane-D path.

- [ ] **Step 7: Final commit.**
  ```bash
  git add console/README.md console/tests/test_readme_contract.py
  git commit -m "docs(console): README (both modes, data-root/t_ref/namespace traps) + manual E2E checklist"
  ```
