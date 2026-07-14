import sqlite3

import pytest
from fastapi.testclient import TestClient

from lean_memory_console.app import create_app
from lean_memory_console.config import ConsoleConfig
from lean_memory_console.engine import EngineGateway
from lean_memory_console.events import EventLog


@pytest.fixture
def docker(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    cfg = ConsoleConfig(
        data_root=root, mode="docker", models="stub", api_key="k",
    )
    log = EventLog(root)
    gw = EngineGateway(cfg, log)
    app = create_app(cfg, gw, log)
    client = TestClient(app)
    # `app` is yielded too so the /mcp tests can open a context-managed client
    # (`with TestClient(app) as c:`) that runs the app lifespan — the MCP session
    # manager is once-only and is started there, not per request.
    yield cfg, client, root, app
    gw.close()
    log.close()


AUTH = {"Authorization": "Bearer k"}


def test_add_search_roundtrip(docker):
    _cfg, client, _root, _app = docker
    r = client.post(
        "/v1/proj/memories",
        headers=AUTH,
        # Text uses a relation verb the offline stub extractor recognizes
        # (works_at); "sails"/"runs" are not in the stub's verb table, so those
        # would extract zero facts. Behaviour under test (add->search roundtrip)
        # is unchanged.
        json={"text": "Helen works at Acme."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["fact_ids"]
    assert "superseded_count" in body

    s = client.post(
        "/v1/proj/search", headers=AUTH, json={"query": "Acme", "k": 5}
    )
    assert s.status_code == 200
    hits = s.json()["hits"]
    assert hits
    # §5 payload keys present on REST hit objects
    for key in (
        "fact_id", "fact_text", "final_score", "relevance", "recency",
        "importance", "dense_rank", "sparse_rank", "rrf_score",
    ):
        assert key in hits[0]


def test_t_ref_to_valid_at(docker):
    _cfg, client, root, _app = docker
    t_ref = 1_600_000_000_000
    r = client.post(
        "/v1/proj/memories",
        headers=AUTH,
        # lives_in is a stub-recognized relation verb; "runs" is not, so it
        # would extract no facts (and the valid_at assertion would be vacuous).
        json={"text": "Ivan lives in Oslo.", "t_ref": t_ref},
    )
    ids = r.json()["fact_ids"]
    con = sqlite3.connect(f"file:{root / 'proj.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT valid_at FROM fact WHERE id IN (%s)" % ",".join("?" * len(ids)),
        ids,
    ).fetchall()
    con.close()
    assert all(row["valid_at"] == t_ref for row in rows)


def test_latest_only_flag_honored(docker):
    _cfg, client, _root, _app = docker
    client.post(
        "/v1/proj/memories", headers=AUTH,
        json={"text": "The user lives in Rome."},
    )
    client.post(
        "/v1/proj/memories", headers=AUTH,
        json={"text": "The user lives in Oslo."},
    )
    latest = client.post(
        "/v1/proj/search", headers=AUTH,
        json={"query": "lives", "latest_only": True},
    ).json()["hits"]
    allhits = client.post(
        "/v1/proj/search", headers=AUTH,
        json={"query": "lives", "latest_only": False},
    ).json()["hits"]
    assert len(allhits) >= len(latest)


def test_reserved_ns_404(docker):
    _cfg, client, _root, _app = docker
    r = client.post(
        "/v1/_events/memories", headers=AUTH, json={"text": "x"}
    )
    assert r.status_code == 404


def test_bearer_required(docker):
    _cfg, client, _root, _app = docker
    r = client.post("/v1/proj/memories", json={"text": "x"})
    assert r.status_code == 401


def test_validation_422(docker):
    _cfg, client, _root, _app = docker
    r = client.post("/v1/proj/memories", headers=AUTH, json={})
    assert r.status_code == 422


def test_mcp_mount_unauthorized_401(docker):
    _cfg, _client, _root, app = docker
    # The bearer gate rejects before any MCP handling; a context-managed client
    # runs the app lifespan (which starts the once-only MCP session manager).
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1}
        )
    assert r.status_code == 401


def test_mcp_mount_exists(docker):
    _cfg, _client, _root, app = docker
    # A valid bearer delegates to the MCP app. Two SEQUENTIAL authenticated
    # requests guard the once-only-run() regression: the session manager is
    # started once by the app lifespan (not per request), so a second call must
    # still succeed. Both must NOT 401; a full protocol round-trip is still the
    # manual E2E's job, but here initialize + tools/list both return 200.
    hdrs = {
        "Authorization": "Bearer k",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r1 = client.post(
            "/mcp",
            headers=hdrs,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        r2 = client.post(
            "/mcp",
            headers=hdrs,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    assert r1.status_code != 401
    assert r2.status_code != 401
    # The second request proves run() was not re-entered per request.
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_mcp_mount_rejects_cross_site_origin(docker):
    _cfg, _client, _root, app = docker
    # Transport security is enabled with a loopback allow-list: a bearer-valid
    # request bearing a disallowed cross-site Origin is rejected by the inner
    # MCP app (403), proving DNS-rebinding protection is active, not disabled.
    hdrs = {
        "Authorization": "Bearer k",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Origin": "http://evil.example",
    }
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post(
            "/mcp",
            headers=hdrs,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert r.status_code == 403


def _mcp_status_for_base_url(tmp_path, base_url, *, mcp_allowed_hosts):
    """Build a fresh docker app (its MCP session manager is once-only, so each
    check needs its own app) and return the /mcp status for one authenticated
    request whose Host derives from base_url."""
    root = tmp_path / "data"
    root.mkdir(parents=True)
    cfg = ConsoleConfig(
        data_root=root,
        mode="docker",
        models="stub",
        api_key="k",
        mcp_allowed_hosts=mcp_allowed_hosts,
    )
    log = EventLog(root)
    gw = EngineGateway(cfg, log)
    app = create_app(cfg, gw, log)
    hdrs = {
        "Authorization": "Bearer k",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    try:
        with TestClient(app, base_url=base_url) as client:
            return client.post("/mcp", headers=hdrs, json=payload).status_code
    finally:
        gw.close()
        log.close()


def test_mcp_allowed_hosts_admits_remote_host(tmp_path):
    # LM_MCP_ALLOWED_HOSTS (here injected via ConsoleConfig.mcp_allowed_hosts)
    # extends the loopback default so a LAN/remote Host is accepted — the shipped
    # compose publishes 8377 directly, no reverse proxy.
    remote = _mcp_status_for_base_url(
        tmp_path / "remote",
        "http://myhost:8377",
        mcp_allowed_hosts=["myhost:*"],
    )
    assert remote == 200


def test_mcp_loopback_default_still_admitted(tmp_path):
    # The loopback default remains admitted alongside any extra pattern.
    loopback = _mcp_status_for_base_url(
        tmp_path / "loopback",
        "http://127.0.0.1",
        mcp_allowed_hosts=["myhost:*"],
    )
    assert loopback == 200


def test_mcp_unlisted_remote_host_rejected(tmp_path):
    # Without the host in the allow-list, a remote Host is rejected (421) — the
    # loopback-only default is the safe baseline.
    unlisted = _mcp_status_for_base_url(
        tmp_path / "unlisted",
        "http://myhost:8377",
        mcp_allowed_hosts=[],
    )
    assert unlisted == 421


def test_static_mount_does_not_shadow_mcp(tmp_path, monkeypatch):
    # Regression: the SPA StaticFiles mount at "/" serves only GET/HEAD and 405s
    # every other method on any path it matches. If it is registered before /mcp
    # (or if a bare POST /mcp is not redirected past it), POST /mcp returns 405
    # instead of the bearer gate's 401. This pins the ordering + slash-redirect
    # with a real static dir present, independent of the UI build.
    import lean_memory_console.app as appmod

    spa = tmp_path / "spa"
    spa.mkdir()
    (spa / "index.html").write_text("<!doctype html><title>console</title>ok")
    monkeypatch.setattr(appmod, "_static_dir", lambda: spa)

    root = tmp_path / "data"
    root.mkdir()
    cfg = ConsoleConfig(data_root=root, mode="docker", models="stub", api_key="k")
    log = EventLog(root)
    gw = EngineGateway(cfg, log)
    app = create_app(cfg, gw, log)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            # POST /mcp without a bearer must reach the gate (401), NOT be
            # swallowed by StaticFiles (405).
            r = client.post(
                "/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1}
            )
            assert r.status_code == 401
            # The SPA is genuinely mounted and served at "/".
            index = client.get("/")
            assert index.status_code == 200
            assert "console" in index.text
    finally:
        gw.close()
        log.close()
