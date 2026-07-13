import shutil

import pytest
from fastapi.testclient import TestClient

from lean_memory_console.app import create_app
from lean_memory_console.config import ConsoleConfig
from lean_memory_console.engine import EngineGateway
from lean_memory_console.events import EventLog
from tests.fixtures.build_fixture import FIXTURE_DIR


@pytest.fixture
def data_root(tmp_path):
    root = tmp_path / "data"
    shutil.copytree(FIXTURE_DIR, root)
    return root


def _first_ns(client, token):
    body = client.get("/views/namespaces", params={"token": token}).json()
    return body[0]["name"]


@pytest.fixture
def local(data_root):
    cfg = ConsoleConfig(
        data_root=data_root, mode="local", models="stub",
        session_token="sesame",
    )
    log = EventLog(data_root)
    gw = EngineGateway(cfg, log)
    app = create_app(cfg, gw, log)
    client = TestClient(app)
    yield cfg, client
    gw.close()
    log.close()


@pytest.fixture
def docker(data_root):
    cfg = ConsoleConfig(
        data_root=data_root, mode="docker", models="stub",
        api_key="secretkey",
    )
    log = EventLog(data_root)
    gw = EngineGateway(cfg, log)
    app = create_app(cfg, gw, log)
    client = TestClient(app)
    yield cfg, client
    gw.close()
    log.close()


def test_whoami_local_no_auth(local):
    _cfg, client = local
    r = client.get("/views/whoami")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "local"
    assert body["auth"] == "token"
    assert body["authenticated"] is False
    assert "data_root" in body


def test_whoami_docker_no_auth(docker):
    _cfg, client = docker
    body = client.get("/views/whoami").json()
    assert body["mode"] == "docker"
    assert body["auth"] == "bearer"
    assert body["authenticated"] is False


def test_whoami_authenticated_local(local):
    _cfg, client = local
    body = client.get("/views/whoami", params={"token": "sesame"}).json()
    assert body["authenticated"] is True


def test_namespaces_requires_token(local):
    _cfg, client = local
    assert client.get("/views/namespaces").status_code == 401


def test_namespaces_bad_bearer(docker):
    _cfg, client = docker
    r = client.get(
        "/views/namespaces", headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code == 401


def test_token_via_header(local):
    _cfg, client = local
    r = client.get(
        "/views/namespaces", headers={"X-Console-Token": "sesame"}
    )
    assert r.status_code == 200


def test_referrer_policy_header(local):
    _cfg, client = local
    r = client.get("/views/whoami")
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_host_spoof_403_local(local):
    _cfg, client = local
    r = client.get(
        "/views/whoami",
        params={"token": "sesame"},
        headers={"Host": "evil.example.com"},
    )
    assert r.status_code == 403


def test_facts_pagination_envelope(local):
    _cfg, client = local
    ns = _first_ns(client, "sesame")
    r = client.get(
        f"/views/{ns}/facts",
        params={"token": "sesame", "page": 1, "page_size": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"items", "page", "page_size", "total"}
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) <= 2


def test_fact_detail_has_chain(local):
    _cfg, client = local
    ns = _first_ns(client, "sesame")
    # find a retired fact via latest_only=false so a chain exists
    facts = client.get(
        f"/views/{ns}/facts",
        params={"token": "sesame", "latest_only": "false", "page_size": 200},
    ).json()["items"]
    fid = facts[0]["id"]
    r = client.get(
        f"/views/{ns}/facts/{fid}", params={"token": "sesame"}
    )
    assert r.status_code == 200
    body = r.json()
    assert "chain" in body
    assert isinstance(body["chain"], list)


def test_events_kind_filter(local):
    _cfg, client = local
    ns = _first_ns(client, "sesame")
    r = client.get(
        f"/views/{ns}/events",
        params={"token": "sesame", "kind": "search"},
    )
    assert r.status_code == 200
    body = r.json()
    assert all(it["kind"] == "search" for it in body["items"])


def test_test_search_records_ui_and_excluded_from_activity(local):
    _cfg, client = local
    ns = _first_ns(client, "sesame")
    before = client.get(
        "/views/namespaces", params={"token": "sesame"}
    ).json()
    before_searches = next(
        n["activity"]["searches"] for n in before if n["name"] == ns
    )
    r = client.post(
        f"/views/{ns}/test-search",
        params={"token": "sesame"},
        json={"query": "test", "k": 3},
    )
    assert r.status_code == 200
    assert "hits" in r.json()
    # the ui-origin search event was recorded ...
    evs = client.get(
        f"/views/{ns}/events",
        params={"token": "sesame", "kind": "search"},
    ).json()
    assert any(it["payload"].get("origin") == "ui" for it in evs["items"])
    # ... but excluded from the 7-day activity searches count
    after = client.get(
        "/views/namespaces", params={"token": "sesame"}
    ).json()
    after_searches = next(
        n["activity"]["searches"] for n in after if n["name"] == ns
    )
    assert after_searches == before_searches
