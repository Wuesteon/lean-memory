"""Route tests for the maintenance review path (WP10b, spec §8.1/§6.3).

The app is driven against a FRESH data root (not the shared fixture copytree)
so proposals can be staged deterministically — either through the gateway's
apply path or with the same direct ``stage_proposal`` helper test_engine uses.
"""

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from lean_memory_console.app import create_app
from lean_memory_console.config import ConsoleConfig
from lean_memory_console.engine import EngineGateway
from lean_memory_console.events import EventLog


@pytest.fixture
def local(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    cfg = ConsoleConfig(
        data_root=root, mode="local", models="stub", session_token="sesame",
    )
    log = EventLog(root)
    gw = EngineGateway(cfg, log)
    app = create_app(cfg, gw, log)
    client = TestClient(app, base_url="http://127.0.0.1")
    yield cfg, client
    gw.close()
    log.close()


def _stage_evict_proposal(root, namespace):
    """Stage one evict proposal directly on the namespace file — the review path's
    input. Dims match FakeEmbedder (768/256), the gateway's stub embedder."""
    from lean_memory.store.sqlite_store import SqliteStore
    from lean_memory.types import Entity, Episode, Fact, new_id, now_ms

    path = root / f"{namespace}.db"
    store = SqliteStore(path, dim=768, coarse_dim=256)
    try:
        ent = store.upsert_entity(Entity(namespace=namespace, name="Zoe", type=None))
        ep = Episode(namespace=namespace, raw="seed", t_ref=now_ms(), source="user")
        store.add_episode(ep)
        fact = Fact(
            id=new_id(), namespace=namespace, subject_id=ent.id, predicate="about",
            object_literal="x", fact_text="Zoe once liked trivia.", valid_at=now_ms(),
            episode_id=ep.id, confidence=1.0, salience=1.0, is_inference=0,
            ingested_at=now_ms(), created_at=now_ms(),
        )
        store.add_fact(
            fact, np.zeros(768, dtype=np.float32), np.zeros(256, dtype=np.float32)
        )
        run_id = store.create_run(namespace, "cli", now_ms(), "hash")
        pid = store.stage_proposal(
            run_id, namespace, "evict",
            json.dumps({"fact_id": fact.id, "fact_text": fact.fact_text}),
            now_ms(), now_ms() + 30 * 86_400_000, "stub",
        )
        store.finish_run(run_id, "ok", now_ms(), None, fact.id)
        return pid, fact.id
    finally:
        store.close()


def _fact_tier(root, namespace, fact_id):
    import sqlite3

    con = sqlite3.connect(f"file:{root / f'{namespace}.db'}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT tier FROM fact WHERE id = ?", (fact_id,)
        ).fetchone()[0]
    finally:
        con.close()


# ── queue ─────────────────────────────────────────────────────────────────────
def test_queue_roundtrip_against_real_staged_proposal(local):
    cfg, client = local
    # Stage a real proposal directly on the namespace file — the queue path's
    # input — exactly as test_engine's maintenance tests do.
    pid, _fid = _stage_evict_proposal(cfg.data_root, "proj")
    r = client.get(
        "/views/proj/review/queue", params={"token": "sesame"}
    )
    assert r.status_code == 200
    groups = r.json()
    ids = [p["id"] for g in groups for p in g["proposals"]]
    assert pid in ids


# ── decide: approve / reject / edit round-trips ─────────────────────────────────
def test_decide_reject_roundtrip(local):
    cfg, client = local
    pid, _fid = _stage_evict_proposal(cfg.data_root, "proj")
    r = client.post(
        f"/views/proj/review/{pid}/decide",
        params={"token": "sesame"},
        json={"decision": "reject"},
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "rejected"
    # Gone from the pending queue.
    groups = client.get(
        "/views/proj/review/queue", params={"token": "sesame"}
    ).json()
    assert all(p["id"] != pid for g in groups for p in g["proposals"])


def test_decide_approve_roundtrip(local):
    cfg, client = local
    pid, _fid = _stage_evict_proposal(cfg.data_root, "proj")
    r = client.post(
        f"/views/proj/review/{pid}/decide",
        params={"token": "sesame"},
        json={"decision": "approve"},
    )
    assert r.status_code == 200
    # An approved evict applies the eviction — an outcome, not a 409.
    assert r.json()["outcome"] not in ("already_decided", "already_applied")


def test_decide_edit_on_evict_is_invalid(local):
    # `edit` is only valid on a summarize proposal (lifecycle §5); on an evict it
    # returns a plain invalid_decision result — still a 200 pass-through here.
    cfg, client = local
    pid, _fid = _stage_evict_proposal(cfg.data_root, "proj")
    r = client.post(
        f"/views/proj/review/{pid}/decide",
        params={"token": "sesame"},
        json={"decision": "edit", "edited_text": "new text"},
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "invalid_decision"


def test_double_decide_returns_409_with_body(local):
    cfg, client = local
    pid, _fid = _stage_evict_proposal(cfg.data_root, "proj")
    first = client.post(
        f"/views/proj/review/{pid}/decide",
        params={"token": "sesame"},
        json={"decision": "reject"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/views/proj/review/{pid}/decide",
        params={"token": "sesame"},
        json={"decision": "reject"},
    )
    assert second.status_code == 409
    body = second.json()
    # Body passed through verbatim: the lifecycle's already-decided shape.
    assert body["outcome"] == "already_decided"
    assert body["proposal_id"] == pid
    assert body["status"] == "rejected"


# ── promote ─────────────────────────────────────────────────────────────────────
def test_promote_flips_tier(local):
    cfg, client = local
    _pid, fid = _stage_evict_proposal(cfg.data_root, "proj")
    r = client.post(
        "/views/proj/review/promote",
        params={"token": "sesame"},
        json={"fact_id": fid},
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "promoted"
    # Verify via a second read against the file.
    assert _fact_tier(cfg.data_root, "proj", fid) == "hot"


# ── status ──────────────────────────────────────────────────────────────────────
def test_status_shape(local):
    cfg, client = local
    _pid, _fid = _stage_evict_proposal(cfg.data_root, "proj")
    r = client.get(
        "/views/proj/maintenance/status", params={"token": "sesame"}
    )
    assert r.status_code == 200
    status = r.json()
    assert status["namespace"] == "proj"
    assert status["runs"] >= 1
    assert status["pending_proposals"] == 1
    assert status["last_run"]["status"] == "ok"


# ── run: dry-run default stages nothing ─────────────────────────────────────────
def test_run_defaults_to_dry_run(local):
    cfg, client = local
    client.post(
        "/v1/proj/memories",
        params={"token": "sesame"},
        json={"text": "I work at Acme."},
    )
    r = client.post(
        "/views/proj/maintenance/run", params={"token": "sesame"}, json={}
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "dry-run"
    # No run persisted by a dry-run.
    status = client.get(
        "/views/proj/maintenance/status", params={"token": "sesame"}
    ).json()
    assert status["runs"] == 0


def test_run_apply_records_run(local):
    cfg, client = local
    client.post(
        "/v1/proj/memories",
        params={"token": "sesame"},
        json={"text": "I work at Acme."},
    )
    r = client.post(
        "/views/proj/maintenance/run",
        params={"token": "sesame"},
        json={"apply": True},
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "apply"


# ── guards: auth + reserved / unknown namespace ─────────────────────────────────
def test_queue_requires_auth(local):
    _cfg, client = local
    assert client.get("/views/proj/review/queue").status_code == 401


def test_status_requires_auth(local):
    _cfg, client = local
    assert client.get("/views/proj/maintenance/status").status_code == 401


def test_reserved_namespace_404(local):
    _cfg, client = local
    r = client.get(
        "/views/_events/review/queue", params={"token": "sesame"}
    )
    assert r.status_code == 404


def test_unknown_namespace_404(local):
    _cfg, client = local
    r = client.get(
        "/views/does-not-exist/review/queue", params={"token": "sesame"}
    )
    assert r.status_code == 404


def test_run_unknown_namespace_404_no_file(local):
    cfg, client = local
    missing = "no_such_namespace_xyz"
    db_path = cfg.data_root / f"{missing}.db"
    assert not db_path.exists()
    r = client.post(
        f"/views/{missing}/maintenance/run",
        params={"token": "sesame"},
        json={},
    )
    assert r.status_code == 404
    # The 404 guard must not create the namespace .db as a side effect.
    assert not db_path.exists()
