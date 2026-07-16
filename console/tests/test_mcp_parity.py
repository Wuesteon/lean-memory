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


# The exact tool set the console stdio wrapper exposes (§6.3): the two memory tools
# plus the four sleep-time maintenance tools. Set equality goes RED the moment the
# wrapper grows or drops a tool — the pin that keeps all three surfaces in lockstep.
_EXPECTED_TOOLS = {
    "memory_add",
    "memory_search",
    "memory_maintenance_run",
    "memory_maintenance_status",
    "memory_review_queue",
    "memory_review_decide",
}


def test_wrapper_exposes_exactly_the_six_tools(tmp_path):
    cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
    log = EventLog(tmp_path)
    gw = EngineGateway(cfg, log)
    wrapper = build_mcp(gw)
    names = set(_tool_params(wrapper).keys())
    gw.close()
    log.close()
    assert names == _EXPECTED_TOOLS


def test_http_mount_exposes_the_same_tool_set(tmp_path):
    """The Docker HTTP mount registers the identical six tools (§6.3 surface parity)."""
    from lean_memory_console.routes.mcp import _build_http_mcp

    cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
    log = EventLog(tmp_path)
    gw = EngineGateway(cfg, log)
    http_mcp = _build_http_mcp(gw, cfg.mcp_allowed_hosts)
    names = set(_tool_params(http_mcp).keys())
    gw.close()
    log.close()
    assert names == _EXPECTED_TOOLS


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
