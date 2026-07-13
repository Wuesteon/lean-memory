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
