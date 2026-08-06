"""Tool metadata contract for the console's TWO MCP surfaces (WP14).

The core stdio server got this treatment in WP13 (tests/test_mcp_tool_metadata.py);
the console ships its own wrapper tools on two surfaces — the stdio server the
plugin runs (``observe_mcp.build_mcp``) and the Docker HTTP mount
(``routes/mcp._build_http_mcp``) — and a directory (Glama et al.) or an agent
grades whatever a surface sends over ``tools/list``. Pinned here, for BOTH
surfaces:

* **Annotations** — ``readOnlyHint`` / ``destructiveHint`` / ``idempotentHint`` /
  ``openWorldHint`` so a client knows what a call does to the world before making
  it. Everything runs against local SQLite under the console's data root, so
  ``openWorldHint`` is False everywhere.
* **Parameter descriptions** — the schema is generated from type hints; bare
  hints ship 0% description coverage. Every parameter must carry a non-empty
  description.
* **When-to-use guidance** — every description must contain a sentence telling an
  agent WHEN to reach for the tool, and the core memory tools must name their
  siblings so a client can choose between alternatives.
* **Surface parity** — the two surfaces must send byte-identical metadata, the
  §6.3 parity rule extended from names/signatures to descriptions + annotations.

Offline: metadata lives on the server registries; the gateway is built with
``models="stub"`` and no tool is ever called.
"""

from __future__ import annotations

import re

import pytest

from lean_memory_console.config import ConsoleConfig
from lean_memory_console.engine import EngineGateway
from lean_memory_console.events import EventLog
from lean_memory_console.observe_mcp import build_mcp
from lean_memory_console.routes.mcp import _build_http_mcp

SURFACES = ("stdio", "http")


@pytest.fixture(scope="module")
def surfaces(tmp_path_factory):
    """{surface: {tool_name: Tool}} for the stdio wrapper and the HTTP mount."""
    root = tmp_path_factory.mktemp("console-tool-metadata")
    cfg = ConsoleConfig(data_root=root, mode="local", models="stub")
    log = EventLog(root)
    gw = EngineGateway(cfg, log)
    http_mcp, _build_app = _build_http_mcp(gw, cfg.mcp_allowed_hosts)
    out = {
        "stdio": {t.name: t for t in build_mcp(gw)._tool_manager.list_tools()},
        "http": {t.name: t for t in http_mcp._tool_manager.list_tools()},
    }
    yield out
    gw.close()
    log.close()


# What each console tool does to the world. readOnlyHint is judged against the MCP
# standard — "does not modify its ENVIRONMENT" — not against memory alone, and the
# console applies that one standard to every tool (no telemetry carve-out):
#   memory_search is NOT read-only. It never adds, supersedes or deletes a FACT, but
#     the observing wrapper appends a 'search' row to the console event log and
#     creates the namespace store file on first touch. Core's memory_search IS
#     read-only — core has no event log — so this is a deliberate divergence.
#     idempotentHint=False: each call appends another event row.
#   memory_review_queue is NOT read-only either: listing lazily expires overdue
#     proposals. It IS idempotent — re-listing lands on the same end state.
#   memory_maintenance_run defaults to dry-run but apply=True writes, so it cannot
#     claim read-only.
#   memory_maintenance_status is the only genuinely read-only tool: a pure ledger
#     read, no event row, no model build.
#   Nothing is destructive: the spine is ADD-only (supersession keeps history) and
#     the console deliberately ships no memory_clear.
EXPECTED_HINTS = {
    "memory_add": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
    "memory_search": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
    "memory_maintenance_run": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
    "memory_maintenance_status": {"readOnlyHint": True},
    "memory_review_queue": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
    "memory_review_decide": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
}

# A "when to use" sentence: a sentence that opens with "Use ...".
_WHEN_TO_USE = re.compile(r"(?:\A|[.!?)\n]\s*)Use\b")


@pytest.mark.parametrize("surface", SURFACES)
def test_metadata_covers_the_whole_tool_surface(surfaces, surface):
    """EXPECTED_HINTS goes RED the moment a console tool is added or dropped."""
    assert set(surfaces[surface]) == set(EXPECTED_HINTS)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("name", sorted(EXPECTED_HINTS))
def test_tool_declares_behavior_annotations(surfaces, surface, name):
    ann = surfaces[surface][name].annotations
    assert ann is not None, f"{surface}/{name}: no ToolAnnotations declared"
    # Wire names, valid on both SDK majors: mcp 1.x fields ARE camelCase;
    # mcp 2.0 renamed them snake_case with camelCase serialization aliases.
    dumped = ann.model_dump(by_alias=True)
    for hint, want in EXPECTED_HINTS[name].items():
        assert dumped.get(hint) is want, f"{surface}/{name}: {hint} should be {want}"
    # Everything operates on local SQLite under the console data root — a closed world.
    assert dumped.get("openWorldHint") is False, (
        f"{surface}/{name}: openWorldHint should be False"
    )


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("name", sorted(EXPECTED_HINTS))
def test_every_parameter_has_a_description(surfaces, surface, name):
    props = (surfaces[surface][name].parameters or {}).get("properties", {})
    assert props, f"{surface}/{name}: schema has no properties"
    missing = [
        p for p, spec in props.items() if not (spec.get("description") or "").strip()
    ]
    assert not missing, f"{surface}/{name}: parameters without descriptions: {missing}"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("name", sorted(EXPECTED_HINTS))
def test_description_carries_when_to_use_guidance(surfaces, surface, name):
    desc = surfaces[surface][name].description or ""
    assert _WHEN_TO_USE.search(desc), (
        f"{surface}/{name}: description has no 'Use ...' when-to-use sentence: {desc!r}"
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_namespace_is_defined_wherever_it_appears(surfaces, surface):
    """Every console tool takes `namespace`; each must say what a namespace IS (an
    isolation key for a separate local store), not just repeat the name."""
    for name, tool in surfaces[surface].items():
        desc = tool.parameters["properties"]["namespace"]["description"]
        assert "isolat" in desc.lower(), (
            f"{surface}/{name}: namespace description lacks isolation semantics: {desc!r}"
        )


@pytest.mark.parametrize("surface", SURFACES)
def test_tools_cross_reference_alternatives(surfaces, surface):
    """'When to use X vs Y' guidance: each tool names the sibling it pairs with."""
    tools = surfaces[surface]
    assert "memory_search" in tools["memory_add"].description
    assert "memory_add" in tools["memory_search"].description
    # The maintenance/review quartet is only usable as a chain — say so.
    assert "memory_review_queue" in tools["memory_maintenance_run"].description
    assert "memory_maintenance_run" in tools["memory_maintenance_status"].description
    assert "memory_review_decide" in tools["memory_review_queue"].description
    assert "memory_review_queue" in tools["memory_review_decide"].description


@pytest.mark.parametrize("surface", SURFACES)
def test_descriptions_disclose_side_effects(surfaces, surface):
    tools = surfaces[surface]
    add = tools["memory_add"].description.lower()
    search = tools["memory_search"].description.lower()
    queue = tools["memory_review_queue"].description.lower()
    # add does NOT store raw text — it extracts facts (surprising, disclose it).
    assert "extract" in add
    # ADD-only supersession, not overwrite — the other surprising bit.
    assert "supersed" in add
    # search must say stored FACTS are untouched...
    assert "never adds, supersedes or deletes" in search
    # ...but must NOT claim read-only: the annotation says False, so the prose has
    # to agree (the observing wrapper writes an event row + the store file).
    assert "not read-only" in search
    # ...and that the console records an observability event for every call.
    assert "event" in search and "event" in add
    # listing expires overdue proposals — the reason it is not read-only.
    assert "expire" in queue
    # First call with the [models]/[extract] extras blocks on a model download —
    # the single most surprising thing about calling these tools (core WP13 pins
    # the same disclosure in tests/test_mcp_tool_metadata.py).
    assert "download" in add or "download" in search


def test_both_surfaces_expose_identical_metadata(surfaces):
    """§6.3 parity, extended from names/signatures to descriptions + annotations:
    stdio and the HTTP mount must send byte-identical tools/list metadata."""
    stdio, http = surfaces["stdio"], surfaces["http"]
    assert set(stdio) == set(http)
    for name in sorted(stdio):
        a, b = stdio[name], http[name]
        assert a.description == b.description, f"{name}: description differs"
        assert a.parameters == b.parameters, f"{name}: inputSchema differs"
        assert a.annotations.model_dump(by_alias=True) == b.annotations.model_dump(
            by_alias=True
        ), f"{name}: annotations differ"
