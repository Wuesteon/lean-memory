"""MCP Registry manifest (server.json) must describe a WORKING install.

Regression for the v0.1.2 launch blocker: `uvx --from lean-memory lean-memory-mcp`
installs only the core dependencies, but the console script imports `mcp` at module
top level (mcp_server.py) — an optional extra — so every registry install crashed on
startup with ModuleNotFoundError. The --from spec must carry the extras the server
actually needs (and ships its calibrated quality with): mcp, models, extract.
"""

import json
import re
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # Python 3.10 — tomli ships via the [dev] extra
    import tomli as tomllib

_ROOT = Path(__file__).resolve().parents[1]

# The canonical tool surface the shipped MCP server exposes (§6.3): the two memory
# tools plus the four sleep-time maintenance tools. The v0.1.3 lesson is that a
# manifest must describe what the server actually exposes — so we pin the set here
# and reconcile both shipped stdio manifests (server.json → core lean-memory-mcp;
# plugin/.mcp.json → console lean-memory-console mcp) against a working install.
_EXPECTED_TOOLS = {
    "memory_add",
    "memory_search",
    "memory_maintenance_run",
    "memory_maintenance_status",
    "memory_review_queue",
    "memory_review_decide",
}


def _manifest() -> dict:
    return json.loads((_ROOT / "server.json").read_text())


def _from_spec(manifest: dict) -> str:
    """The value of the uvx `--from` runtime argument (the pip requirement spec)."""
    args = manifest["packages"][0]["runtimeArguments"]
    from_args = [a for a in args if a.get("name") == "--from"]
    assert len(from_args) == 1, "expected exactly one --from runtime argument"
    return from_args[0]["value"]


def test_registry_install_spec_carries_required_extras():
    spec = _from_spec(_manifest())
    m = re.fullmatch(r"lean-memory\[([a-z0-9_,-]+)\]", spec)
    assert m, f"--from spec {spec!r} must be lean-memory[<extras>] — bare installs crash"
    extras = set(m.group(1).split(","))
    # mcp: the server cannot even import without it. models/extract: the canonical
    # real-quality install the README/registry description promises (gate item 1).
    assert {"mcp", "models", "extract"} <= extras


def test_manifest_versions_match_pyproject():
    manifest = _manifest()
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]
    assert manifest["version"] == version
    assert manifest["packages"][0]["version"] == version


def test_package_dunder_version_matches_pyproject():
    import lean_memory

    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert lean_memory.__version__ == pyproject["project"]["version"]


# ── manifest ⇄ server-surface reconciliation (§6.3, the v0.1.3 lesson) ─────────
def _core_tool_names() -> set[str]:
    """The exact tool set the core stdio server (server.json's entry point) exposes."""
    pytest.importorskip("mcp", reason="optional [mcp] extra not installed")
    import lean_memory.mcp_server as srv

    return {t.name for t in srv.mcp._tool_manager.list_tools()}


def test_core_server_exposes_the_maintenance_tools_plus_clear():
    """What server.json ships (lean-memory-mcp) exposes the pinned six-tool set PLUS
    memory_clear (core-only; the console deliberately omits the deletion surface, §6).
    The four maintenance tools must all be present with names identical to the console
    surfaces — the reconciliation the v0.1.3 lesson demands."""
    names = _core_tool_names()
    assert _EXPECTED_TOOLS <= names, f"missing maintenance/memory tools: {_EXPECTED_TOOLS - names}"
    assert names == _EXPECTED_TOOLS | {"memory_clear"}


def test_server_json_entrypoint_is_the_core_stdio_script():
    """server.json's packageArgument names the core stdio console script, whose module
    is the one the tool-surface pin above checks — the manifest ⇄ surface link."""
    manifest = _manifest()
    pkg_args = manifest["packages"][0]["packageArguments"]
    values = [a.get("value") for a in pkg_args]
    assert "lean-memory-mcp" in values
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert scripts["lean-memory-mcp"] == "lean_memory.mcp_server:main"


def test_maintain_script_is_declared_for_auto_spawn():
    """Auto-spawn (§6.5) invokes `python -m lean_memory.maintain.cli`; the same code is
    exposed as the lean-memory-maintain console script. Pin its declaration so the
    module path the spawn relies on cannot silently move."""
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert scripts["lean-memory-maintain"] == "lean_memory.maintain.cli:main"


def test_plugin_mcp_json_ships_the_console_stdio_server():
    """plugin/.mcp.json ships `lean-memory-console mcp` — the console stdio server whose
    six-tool surface is pinned in console/tests/test_mcp_parity.py. Both shipped stdio
    manifests thus resolve to a server exposing the identical pinned tool set (§6.3)."""
    data = json.loads((_ROOT / "plugin" / ".mcp.json").read_text())
    entry = data["mcpServers"]["lean-memory"]
    assert entry["command"] == "uvx"
    assert entry["args"] == ["lean-memory-console", "mcp"]
