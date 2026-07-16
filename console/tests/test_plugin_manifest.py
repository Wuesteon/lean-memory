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
    assert data["version"] == "0.2.0"
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


def test_all_commands_exist_with_frontmatter() -> None:
    cmds = {
        "memory-ui.md": "/memory:ui",
        "memory-status.md": "/memory:status",
        "memory-server-up.md": "/memory:server-up",
        "memory-server-down.md": "/memory:server-down",
        "review-memory.md": "/memory:review-memory",
    }
    for filename in cmds:
        path = PLUGIN / "commands" / filename
        assert path.is_file(), f"missing command file {filename}"
        text = path.read_text()
        # YAML frontmatter block.
        assert text.startswith("---\n"), f"{filename} must open with frontmatter"
        assert "description:" in text.split("---", 2)[1]


def test_review_memory_command_forbids_agent_decisions() -> None:
    """The review command must carry the §6.4 guardrail: no agent-initiated decisions
    without an explicit user verdict, and no batch verb inferred from silence."""
    text = (PLUGIN / "commands" / "review-memory.md").read_text().lower()
    assert "may not decide" in text
    assert "explicit user verdict" in text
    assert "silence is not consent" in text
    # It drives the two review tools by name.
    assert "memory_review_queue" in text
    assert "memory_review_decide" in text


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


def test_no_print_status_dead_invocation_anywhere_in_plugin() -> None:
    # `serve --print-status` is not a real CLI flag; guard against it creeping
    # back into any plugin command or manifest.
    for path in PLUGIN.rglob("*"):
        if path.is_file():
            assert "--print-status" not in path.read_text(errors="ignore"), (
                f"dead --print-status invocation found in {path}"
            )
