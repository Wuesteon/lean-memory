# WP12 mcp 2.0 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support mcp SDK 1.x AND 2.x (dual-path), widening the pin from `>=1.2,<2` to `>=1.2,<3` — lifting the emergency cap shipped in v0.2.2 without abandoning 1.x-pinned environments one day into the 2.0 ecosystem.

**Architecture:** mcp 2.0 renamed `mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer` (same decorator API), moved transport params from the constructor to `streamable_http_app(...)`, added ctor `version=`, and replaced the in-memory test helper `mcp.shared.memory.create_connected_server_and_client_session` with `mcp.client.Client(server)`. Each package gets a small private compat module (deliberately duplicated — console↔core version skew makes cross-package private imports fragile); tests get one shared `client_session` helper. CI resolves 2.0 (fresh install) so it exercises the v2 path; the dev venv holds 1.x and exercises the v1 path.

**Tech Stack:** verified against mcp 2.0.0 by direct introspection: `MCPServer(name, version=...)`; `.tool()/.prompt(name=...)/.run()` unchanged; `streamable_http_app(*, streamable_http_path, json_response, stateless_http, transport_security, ...)`; `session_manager` property retained; `mcp.server.transport_security` unmoved; `Client(server).call_tool(name, args) -> CallToolResult` with `.content[0].text`.

## Global Constraints

- Offline suite green under BOTH SDK majors: v1 via the dev venv (`PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/ -q`), v2 via a scratch venv with `mcp==2.0.0` installed.
- First-run path behavior unchanged on both majors (same tools, same names, server reports `__version__`).
- Offline-by-default, ADD-only, Apache-2.0, emoji-conventional commits — per workpackets global invariants.

---

### Task 1: Core compat module + `mcp_server.py` migration

**Files:**
- Create: `src/lean_memory/_mcp_compat.py`
- Modify: `src/lean_memory/mcp_server.py:25` (import), `:91-95` (construction), nothing else
- Test: existing `tests/test_mcp_server.py` (+ v2-venv run)

**Interfaces:**
- Produces: `_mcp_compat.MCP_V2: bool`; `_mcp_compat.MCPServerType` (the class, for type hints); `_mcp_compat.make_stdio_server(name: str, *, version: str) -> MCPServerType`.

- [ ] **Step 1 (RED): demonstrate the failure under mcp 2.0** — in the v2 scratch venv, `python -m pytest tests/test_mcp_server.py -q` → collection error `No module named 'mcp.server.fastmcp'`.

- [ ] **Step 2: implement**

```python
"""Dual-path mcp SDK compatibility (WP12): the 2.0 SDK renamed FastMCP →
MCPServer and moved the ctor `version=` in; 1.x needs the private
`_mcp_server.version` poke. Marker import is the v2-only module — cheap and
unambiguous. Duplicated in lean_memory_console._mcp_compat on purpose:
console↔core version skew makes cross-package private imports fragile."""

from __future__ import annotations

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as MCPServerType
    MCP_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServerType  # type: ignore[assignment]
    MCP_V2 = False


def make_stdio_server(name: str, *, version: str) -> MCPServerType:
    if MCP_V2:
        return MCPServerType(name, version=version)
    server = MCPServerType(name)
    # 1.x FastMCP doesn't take a version; unset, the SDK reports ITS OWN
    # version in the initialize handshake.
    server._mcp_server.version = version
    return server
```

In `mcp_server.py`: replace the `from mcp.server.fastmcp import FastMCP` import with `from ._mcp_compat import make_stdio_server`, and the `mcp = FastMCP(...)` + version-poke block with `mcp = make_stdio_server("lean-memory", version=__version__)`.

- [ ] **Step 3 (GREEN v1):** dev-venv `pytest tests/ -q` all pass. Commit.

---

### Task 2: Test-side client compat + v2 suite green

**Files:**
- Create: `tests/mcp_client_compat.py`
- Modify: `tests/test_mcp_server.py`, `tests/test_mcp_maintenance_tools.py`, `tests/test_maintenance_cli.py`, `tests/test_stdout_hygiene.py` (swap the helper import; call sites keep `session.call_tool` / `result.content[0].text` — identical on both majors)

**Interfaces:**
- Produces: `client_session(mcp_obj)` — async context manager yielding an object with `.call_tool(name, args) -> CallToolResult`; v2 wraps `mcp.client.Client(mcp_obj)`, v1 wraps `create_connected_server_and_client_session(mcp_obj._mcp_server)`.

- [ ] Implement the helper (branch on the same `mcp.server.mcpserver` marker import), update the four test files, dev-venv suite green.
- [ ] Build the v2 scratch venv (`uv pip install -e '.[dev,mcp]'` then `uv pip install mcp==2.0.0`) and run the FULL suite there — green is the packet's core acceptance gate. Commit.

---

### Task 3: Console migration

**Files:**
- Create: `console/src/lean_memory_console/_mcp_compat.py` (same content as core's, plus `make_streamable_http_app(server, *, streamable_http_path, json_response, stateless_http, transport_security)` returning the ASGI app: v2 passes params to `server.streamable_http_app(...)`; v1 expects them already given to the FastMCP ctor and calls `server.streamable_http_app()`)
- Modify: `console/src/lean_memory_console/observe_mcp.py`, `mcp_tools.py` (type-hint import swap), `routes/mcp.py` (branch: v1 puts transport params in ctor, v2 defers them to the app call)

**Interfaces:**
- Consumes: v2 `streamable_http_app` kwargs verified above; `session_manager` property (unchanged both majors).

- [ ] Implement; console suite green in the console venv (v1). Run console tests under the v2 scratch venv too (install console editable there). Commit.

---

### Task 4: Pins, guard test, changelog

**Files:**
- Modify: `pyproject.toml` (`mcp>=1.2,<2` → `mcp>=1.2,<3`, comment rewritten), `console/pyproject.toml` (same), `tests/test_mcp_server.py` guard (`req.endswith(",<2")` → `",<3"`), `CHANGELOG.md` (Unreleased → Changed)

- [ ] Update all four; both suites green in both venvs; commit. CI (fresh resolve → mcp 2.0.x) is the final v2 gate on the PR.

---

## Verification

- Dev venv (mcp 1.x): core + console suites green.
- v2 scratch venv (mcp 2.0.0): core + console suites green.
- CI matrix green (resolves 2.0 → exercises the v2 path on all six legs).
