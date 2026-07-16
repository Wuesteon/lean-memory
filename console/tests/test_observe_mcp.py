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
    # "owns" is a verb the offline stub extractor supports (yields a fact);
    # do NOT regress this to e.g. "drives", which extracts nothing on the stub
    # path and would make the fact_ids/hits assertions below fail on data.
    add_out = _unwrap(await mcp.call_tool("memory_add", {
        "namespace": "proj", "text": "Frank owns a red car."
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


# ── maintenance tools + review prompt on the stdio wrapper (§6.3, §6.4) ────────
@pytest.mark.anyio
async def test_maintenance_run_dry_by_default(wrapper):
    mcp, _root = wrapper
    await mcp.call_tool("memory_add", {"namespace": "proj", "text": "Frank owns a red car."})
    out = _unwrap(await mcp.call_tool("memory_maintenance_run", {"namespace": "proj"}))
    assert out["mode"] == "dry-run"


@pytest.mark.anyio
async def test_review_queue_tool_returns_groups(wrapper):
    mcp, _root = wrapper
    out = _unwrap(await mcp.call_tool("memory_review_queue", {"namespace": "empty"}))
    # No proposals staged for a fresh namespace → an empty groups list, not an error.
    assert out["groups"] == []


@pytest.mark.anyio
async def test_maintenance_status_tool_shape(wrapper):
    mcp, _root = wrapper
    out = _unwrap(await mcp.call_tool("memory_maintenance_status", {"namespace": "fresh"}))
    # Model-free ledger read, same shape as the core server's status.
    assert out["namespace"] == "fresh"
    assert out["runs"] == 0
    assert out["pending_proposals"] == 0
    assert out["last_run"] is None


def test_review_prompt_is_registered(wrapper):
    mcp, _root = wrapper
    names = {p.name for p in mcp._prompt_manager.list_prompts()}
    assert "review-memory-maintenance" in names


@pytest.mark.anyio
async def test_review_prompt_forbids_agent_deciding(wrapper):
    mcp, _root = wrapper
    rendered = await mcp.get_prompt("review-memory-maintenance", {"namespace": "proj"})
    text = " ".join(
        m.content.text for m in rendered.messages if hasattr(m.content, "text")
    ).lower()
    # The hard rule: no agent-initiated decisions without an explicit user verdict.
    assert "may not decide" in text or "explicit user verdict" in text
    assert "silence is not consent" in text
