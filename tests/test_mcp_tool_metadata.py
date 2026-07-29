"""Tool metadata contract for the MCP server (WP13).

MCP directories (e.g. Glama) grade each tool on exactly what the server sends
over `tools/list`: the description and the inputSchema. Three things they (and
real agents) need are pinned here:

* **Annotations** — `readOnlyHint` / `destructiveHint` / `idempotentHint` /
  `openWorldHint` so a client knows what a call does to the world before
  making it.
* **Parameter descriptions** — FastMCP builds the schema from type hints;
  bare hints ship a schema with 0% description coverage. Every parameter
  must carry a non-empty description.
* **Usage guidance** — each memory tool's description names its sibling
  tools so an agent can pick between alternatives, and discloses non-obvious
  behavior (extraction on add, the first-call model download).

Offline: metadata lives on the FastMCP registry; no Memory is ever built.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="optional [mcp] extra not installed")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    import os

    os.environ.setdefault("LM_DATA_ROOT", str(tmp_path_factory.mktemp("lm")))
    import lean_memory.mcp_server as srv

    return {t.name: t for t in srv.mcp._tool_manager.list_tools()}


# What each tool does to the world. memory_review_queue is NOT read-only:
# listing lazily expires overdue proposals (a write). memory_maintenance_run
# defaults to dry-run but apply=True writes, so it cannot claim read-only.
EXPECTED_HINTS = {
    "memory_add": {"readOnlyHint": False, "destructiveHint": False},
    "memory_search": {"readOnlyHint": True},
    "memory_clear": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
    "memory_maintenance_run": {"readOnlyHint": False, "destructiveHint": False},
    "memory_maintenance_status": {"readOnlyHint": True},
    "memory_review_queue": {"readOnlyHint": False, "destructiveHint": False},
    "memory_review_decide": {"readOnlyHint": False, "destructiveHint": False},
}


def test_metadata_covers_the_whole_tool_surface(tools):
    """EXPECTED_HINTS goes RED the moment a tool is added or dropped."""
    assert set(tools) == set(EXPECTED_HINTS)


@pytest.mark.parametrize("name", sorted(EXPECTED_HINTS))
def test_tool_declares_behavior_annotations(tools, name):
    ann = tools[name].annotations
    assert ann is not None, f"{name}: no ToolAnnotations declared"
    # Wire names, valid on both SDK majors: mcp 1.x fields ARE camelCase;
    # mcp 2.0 renamed them snake_case with camelCase serialization aliases.
    dumped = ann.model_dump(by_alias=True)
    for hint, want in EXPECTED_HINTS[name].items():
        assert dumped.get(hint) is want, f"{name}: {hint} should be {want}"
    # Everything operates on local SQLite under LM_DATA_ROOT — a closed world.
    assert dumped.get("openWorldHint") is False, f"{name}: openWorldHint should be False"


@pytest.mark.parametrize("name", sorted(EXPECTED_HINTS))
def test_every_parameter_has_a_description(tools, name):
    props = (tools[name].parameters or {}).get("properties", {})
    assert props, f"{name}: schema has no properties"
    missing = [p for p, spec in props.items() if not spec.get("description", "").strip()]
    assert not missing, f"{name}: parameters without descriptions: {missing}"


def test_namespace_is_defined_wherever_it_appears(tools):
    """Every tool takes `namespace`; each must say what a namespace IS (an
    isolation key for a separate local store), not just repeat the name."""
    for name, tool in tools.items():
        desc = tool.parameters["properties"]["namespace"]["description"]
        assert "isolat" in desc.lower(), f"{name}: namespace description lacks isolation semantics: {desc!r}"


def test_memory_tools_cross_reference_alternatives(tools):
    """'When to use X vs Y' guidance: each core memory tool names a sibling."""
    assert "memory_search" in tools["memory_add"].description
    assert "memory_add" in tools["memory_search"].description
    assert "memory_clear" in tools["memory_search"].description or (
        "memory_clear" in tools["memory_add"].description
    )


def test_descriptions_disclose_side_effects(tools):
    add = tools["memory_add"].description
    search = tools["memory_search"].description
    # add does NOT store raw text — it extracts facts (surprising, disclose it).
    assert "extract" in add.lower()
    # First call with the [models]/[extract] extras downloads model weights.
    assert "download" in add.lower() or "download" in search.lower()
    # search must say it never modifies stored memory.
    assert "read-only" in search.lower()
