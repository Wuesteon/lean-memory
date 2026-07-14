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
