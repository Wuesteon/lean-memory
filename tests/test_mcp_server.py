"""Tests for the lean-memory MCP server.

All tests run offline: the server is constructed with the default stub backends
(FakeEmbedder + IdentityReranker) by pointing LM_DATA_ROOT at a tmp dir BEFORE
the module is imported, and by importing inside the tests so each test gets a
fresh module-level Memory rooted at its own tmp dir.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_pyproject_declares_mcp_extra_and_script():
    data = tomllib.loads(PYPROJECT.read_text())
    extras = data["project"]["optional-dependencies"]
    assert "mcp" in extras, "an 'mcp' optional extra must be declared"
    assert any(req.startswith("mcp>=1.0") for req in extras["mcp"]), extras["mcp"]
    scripts = data["project"].get("scripts", {})
    assert scripts.get("lean-memory-mcp") == "lean_memory.mcp_server:main"
