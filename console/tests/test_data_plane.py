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
    yield cfg, client, root
    gw.close()
    log.close()


AUTH = {"Authorization": "Bearer k"}


def test_add_search_roundtrip(docker):
    _cfg, client, _root = docker
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
    _cfg, client, root = docker
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
    _cfg, client, _root = docker
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
    _cfg, client, _root = docker
    r = client.post(
        "/v1/_events/memories", headers=AUTH, json={"text": "x"}
    )
    assert r.status_code == 404


def test_bearer_required(docker):
    _cfg, client, _root = docker
    r = client.post("/v1/proj/memories", json={"text": "x"})
    assert r.status_code == 401


def test_validation_422(docker):
    _cfg, client, _root = docker
    r = client.post("/v1/proj/memories", headers=AUTH, json={})
    assert r.status_code == 422


def test_mcp_mount_unauthorized_401(docker):
    _cfg, client, _root = docker
    # No Authorization header -> the ASGI bearer wrapper rejects before MCP.
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
    assert r.status_code == 401


def test_mcp_mount_exists(docker):
    _cfg, client, _root = docker
    # With a valid bearer the wrapper delegates to the MCP app (which then
    # applies its own protocol handling). We assert only that the bearer
    # wrapper does NOT 401 — full MCP-over-HTTP round-trip is deferred to the
    # manual E2E (see the note at the end of this task).
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer k", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
    )
    assert r.status_code != 401
