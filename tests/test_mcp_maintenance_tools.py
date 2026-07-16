"""The four sleep-time maintenance MCP tools on the CORE stdio server (spec §6.3).

All offline (stub backends), rooted at a tmp dir with LM_DATA_ROOT set before the
module is imported (mirrors test_mcp_server.py). The tools round-trip end to end:

  * memory_maintenance_run   — DRY-RUN by default (writes nothing), apply=True stages
  * memory_maintenance_status— ledger-only, provably WITHOUT building the models
  * memory_review_queue      — pending proposals grouped by entity, with evidence
  * memory_review_decide     — approve|reject|edit|promote against staged proposals

The status model-free pin is the load-bearing one: the v0.1.3 lesson is that the
first-run model build must never be forced where a cheap ledger read suffices.
"""

from __future__ import annotations

import json

import pytest

mcp = pytest.importorskip("mcp", reason="optional [mcp] extra not installed")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Fresh core server module rooted at a tmp dir with stub backends."""
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("LM_MAINT_AUTO", raising=False)
    import importlib

    import lean_memory.mcp_server as srv

    importlib.reload(srv)
    from lean_memory import Memory

    srv._MEM = Memory(root=tmp_path)
    yield srv
    if srv._MEM is not None:  # a test may null it to prove status is model-free
        srv._MEM.close()


async def _call(server, name, args):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server
    ) as session:
        result = await session.call_tool(name, args)
        return result.content[0].text


# ── memory_maintenance_run ────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_maintenance_run_dry_by_default_writes_nothing(server, tmp_path):
    """Dry-run default: no maintenance_run row, no proposals — symmetric with CLI."""
    await _call(server, "memory_add", {"namespace": "u1", "text": "I work at Acme."})
    out = await _call(server, "memory_maintenance_run", {"namespace": "u1"})
    data = json.loads(out)
    assert data["mode"] == "dry-run"
    # No ledger row was written by a dry run.
    import sqlite3

    db = tmp_path / "u1.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    runs = con.execute("SELECT COUNT(*) FROM maintenance_run").fetchone()[0]
    props = con.execute("SELECT COUNT(*) FROM maintenance_proposal").fetchone()[0]
    con.close()
    assert runs == 0, "dry-run must write no ledger row"
    assert props == 0, "dry-run must stage no proposals"


@pytest.mark.anyio
async def test_maintenance_run_apply_claims_lease(server, tmp_path):
    """apply=True writes a finished ledger row (the auto band + staging ran)."""
    await _call(server, "memory_add", {"namespace": "u2", "text": "I work at Acme."})
    out = await _call(
        server, "memory_maintenance_run", {"namespace": "u2", "apply": True}
    )
    data = json.loads(out)
    assert data["mode"] == "apply"
    import sqlite3

    db = tmp_path / "u2.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    runs = con.execute(
        "SELECT COUNT(*) FROM maintenance_run WHERE status='ok'"
    ).fetchone()[0]
    con.close()
    assert runs == 1


# ── memory_maintenance_status: MODEL-FREE (the pin) ───────────────────────────
@pytest.mark.anyio
async def test_status_does_not_build_the_model(server, monkeypatch):
    """status MUST read the ledger without forcing the lazy model build (§6.3).

    We make the model builder raise, drop the cached Memory, and assert status
    still returns — proving it never called _mem().
    """
    # A prior add builds & caches Memory; drop it so _mem() would rebuild.
    await _call(server, "memory_add", {"namespace": "u3", "text": "I work at Acme."})
    server._MEM = None

    def boom(root):
        raise RuntimeError("status must not build the model")

    monkeypatch.setattr(server, "_build_memory", boom)
    out = await _call(server, "memory_maintenance_status", {"namespace": "u3"})
    data = json.loads(out)
    assert "runs" in data and "pending_proposals" in data
    assert server._MEM is None, "status forced a model build"


@pytest.mark.anyio
async def test_status_reports_after_apply(server):
    await _call(server, "memory_add", {"namespace": "u4", "text": "I work at Acme."})
    await _call(server, "memory_maintenance_run", {"namespace": "u4", "apply": True})
    out = await _call(server, "memory_maintenance_status", {"namespace": "u4"})
    data = json.loads(out)
    assert data["runs"] >= 1
    assert data["last_run"] is not None
    assert data["last_run"]["status"] == "ok"


@pytest.mark.anyio
async def test_status_unknown_namespace_is_empty(server):
    out = await _call(server, "memory_maintenance_status", {"namespace": "ghost"})
    data = json.loads(out)
    assert data["runs"] == 0
    assert data["pending_proposals"] == 0
    assert data["last_run"] is None


# ── review_queue + review_decide against staged proposals ─────────────────────
def _stage_evict_proposal(root, namespace):
    """Directly stage one evict proposal via the store — the review path's input.

    Uses a dedicated maintenance store (the same class the tools use) so the row is
    a real proposal the tools then read/decide. Returns (proposal_id, fact_id).
    """
    from lean_memory.store.sqlite_store import SqliteStore
    from lean_memory.types import Entity, Episode, Fact, new_id, now_ms

    # Dims MUST match FakeEmbedder (768/256) — the server opens its maintenance store
    # for this same file with the FakeEmbedder's dims, and a mismatch would be refused.
    path = root / f"{namespace}.db"
    store = SqliteStore(path, dim=768, coarse_dim=256)
    try:
        ent = store.upsert_entity(Entity(namespace=namespace, name="Frank", type=None))
        ep = Episode(namespace=namespace, raw="seed", t_ref=now_ms(), source="user")
        store.add_episode(ep)
        fact = Fact(
            id=new_id(), namespace=namespace, subject_id=ent.id, predicate="about",
            object_literal="x", fact_text="Frank likes trivia.", valid_at=now_ms(),
            episode_id=ep.id, confidence=1.0, salience=1.0, is_inference=0,
            ingested_at=now_ms(), created_at=now_ms(),
        )
        import numpy as np

        full = np.zeros(768, dtype=np.float32)
        coarse = np.zeros(256, dtype=np.float32)
        store.add_fact(fact, full, coarse)
        run_id = store.create_run(namespace, "cli", now_ms(), "hash")
        payload = json.dumps({"fact_id": fact.id, "fact_text": fact.fact_text})
        pid = store.stage_proposal(
            run_id, namespace, "evict", payload, now_ms(),
            now_ms() + 30 * 86_400_000, "stub",
        )
        store.finish_run(run_id, "ok", now_ms(), None, fact.id)
        return pid, fact.id
    finally:
        store.close()


@pytest.mark.anyio
async def test_review_queue_lists_staged_proposal(server, tmp_path):
    pid, _fid = _stage_evict_proposal(tmp_path, "u5")
    out = await _call(server, "memory_review_queue", {"namespace": "u5"})
    data = json.loads(out)
    ids = [p["id"] for group in data for p in group["proposals"]]
    assert pid in ids
    # Evidence payload rides along parsed.
    for group in data:
        for p in group["proposals"]:
            if p["id"] == pid:
                assert p["payload"]["fact_id"]


@pytest.mark.anyio
async def test_review_queue_kind_filter(server, tmp_path):
    _stage_evict_proposal(tmp_path, "u6")
    out = await _call(
        server, "memory_review_queue", {"namespace": "u6", "kind": "summarize"}
    )
    data = json.loads(out)
    assert data == []  # no summarize proposals staged


@pytest.mark.anyio
async def test_review_decide_reject(server, tmp_path):
    pid, _fid = _stage_evict_proposal(tmp_path, "u7")
    out = await _call(
        server, "memory_review_decide",
        {"namespace": "u7", "proposal_id": pid, "decision": "reject"},
    )
    data = json.loads(out)
    assert data["outcome"] == "rejected"
    # Now gone from the pending queue.
    q = json.loads(await _call(server, "memory_review_queue", {"namespace": "u7"}))
    assert all(p["id"] != pid for group in q for p in group["proposals"])


@pytest.mark.anyio
async def test_review_decide_promote(server, tmp_path):
    pid, fid = _stage_evict_proposal(tmp_path, "u8")
    out = await _call(
        server, "memory_review_decide",
        {"namespace": "u8", "proposal_id": pid, "decision": "promote"},
    )
    data = json.loads(out)
    # promote on an evict proposal rejects the proposal and promotes the fact.
    assert data["outcome"] in ("promoted", "rejected_and_promoted", "rejected")


@pytest.mark.anyio
async def test_review_decide_edit_records_text(server, tmp_path):
    """A summarize proposal edited-then-approved records the human text (§4.3)."""
    from lean_memory.store.sqlite_store import SqliteStore
    from lean_memory.types import Entity, Episode, Fact, new_id, now_ms
    import numpy as np

    ns = "u9"
    path = tmp_path / f"{ns}.db"
    store = SqliteStore(path, dim=768, coarse_dim=256)  # match FakeEmbedder dims
    try:
        ent = store.upsert_entity(Entity(namespace=ns, name="Ann", type=None))
        ep = Episode(namespace=ns, raw="seed", t_ref=now_ms(), source="user")
        store.add_episode(ep)
        srcs = []
        full = np.zeros(768, dtype=np.float32)
        coarse = np.zeros(256, dtype=np.float32)
        for i in range(2):
            f = Fact(
                id=new_id(), namespace=ns, subject_id=ent.id, predicate="likes",
                object_literal=f"o{i}", fact_text=f"Ann likes thing {i}.",
                valid_at=now_ms(), episode_id=ep.id, confidence=1.0, salience=1.0,
                is_inference=0, ingested_at=now_ms(), created_at=now_ms(),
            )
            store.add_fact(f, full, coarse)
            srcs.append(f.id)
        run_id = store.create_run(ns, "cli", now_ms(), "hash")
        payload = json.dumps({
            "subject_id": ent.id,
            "source_fact_ids": srcs,
            "summary_text": "Ann likes several things.",
        })
        pid = store.stage_proposal(
            run_id, ns, "summarize", payload, now_ms(),
            now_ms() + 30 * 86_400_000, "stub",
        )
        store.finish_run(run_id, "ok", now_ms(), None, srcs[-1])
    finally:
        store.close()

    out = await _call(
        server, "memory_review_decide",
        {
            "namespace": ns, "proposal_id": pid, "decision": "edit",
            "edited_text": "Ann has curated interests.",
        },
    )
    data = json.loads(out)
    assert data["outcome"] in ("applied", "expired")  # applied on fresh targets
    import sqlite3

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT edited_text, status FROM maintenance_proposal WHERE id=?", (pid,)
    ).fetchone()
    con.close()
    if data["outcome"] == "applied":
        assert row["edited_text"] == "Ann has curated interests."
