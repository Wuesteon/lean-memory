import importlib
import sqlite3

import pytest

from lean_memory_console.config import ConsoleConfig
from lean_memory_console.engine import (
    AddResult,
    EngineGateway,
    SearchResult,
    _build_memory,
    resolved_models_mode,
    retry_busy,
)
from lean_memory_console.events import EventLog


def _config(tmp_path):
    return ConsoleConfig(data_root=tmp_path, mode="local", models="stub")


def _models_installed() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


def test_build_memory_stub_uses_fake_embedder(tmp_path):
    cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
    mem = _build_memory(cfg)
    try:
        # models="stub" always pins the deterministic offline embedder.
        assert type(mem.embedder).__name__ == "FakeEmbedder"
    finally:
        mem.close()


def test_build_memory_auto_falls_back_to_stub_when_extras_absent(tmp_path):
    # The console venv has NO [models] extra; auto must degrade gracefully to
    # the stub backend rather than raise.
    if _models_installed():
        pytest.skip("[models] extra installed — auto would select real backends")
    cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="auto")
    mem = _build_memory(cfg)
    try:
        assert type(mem.embedder).__name__ == "FakeEmbedder"
    finally:
        mem.close()


def test_resolved_models_mode_stub_is_stub(tmp_path):
    cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
    assert resolved_models_mode(cfg) == "stub"


def test_resolved_models_mode_auto_reflects_import_availability(tmp_path):
    cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="auto")
    expected = "real" if _models_installed() else "stub"
    assert resolved_models_mode(cfg) == expected


def test_resolved_models_mode_auto_selects_real_when_importable(
    tmp_path, monkeypatch
):
    # Assert the SELECTION LOGIC (not a real model load): with the import made
    # to succeed, auto resolves to "real". The console venv lacks the extra, so
    # we inject a stand-in module so `import sentence_transformers` succeeds.
    import sys
    import types

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.ModuleType("sentence_transformers")
    )
    cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="auto")
    assert resolved_models_mode(cfg) == "real"


def test_resolved_models_mode_stub_ignores_importable_extras(
    tmp_path, monkeypatch
):
    # Even if the extra were importable, models="stub" pins "stub".
    import sys
    import types

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.ModuleType("sentence_transformers")
    )
    cfg = ConsoleConfig(data_root=tmp_path, mode="local", models="stub")
    assert resolved_models_mode(cfg) == "stub"


@pytest.fixture
def gateway(tmp_path):
    cfg = _config(tmp_path)
    log = EventLog(tmp_path)
    gw = EngineGateway(cfg, log)
    yield gw
    gw.close()
    log.close()


def test_retry_busy_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert retry_busy(flaky, attempts=3) == "ok"
    assert calls["n"] == 3


def test_retry_busy_reraises_non_lock():
    def boom():
        raise sqlite3.OperationalError("no such table: fact")

    with pytest.raises(sqlite3.OperationalError):
        retry_busy(boom, attempts=3)


@pytest.mark.anyio
async def test_add_returns_fact_ids(gateway):
    res = await gateway.add("proj", "Alice works at Acme.")
    assert isinstance(res, AddResult)
    assert res.fact_ids
    assert all(isinstance(fid, str) for fid in res.fact_ids)
    assert res.duration_ms >= 0.0


@pytest.mark.anyio
async def test_reserved_namespace_rejected(gateway):
    with pytest.raises(ValueError):
        await gateway.add("_events", "nope")


@pytest.mark.anyio
async def test_t_ref_propagates_to_valid_at(gateway, tmp_path):
    t_ref = 1_600_000_000_000
    res = await gateway.add("proj", "Bob likes coffee.", t_ref=t_ref)
    db = tmp_path / "proj.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT valid_at FROM fact WHERE id IN (%s)"
        % ",".join("?" * len(res.fact_ids)),
        res.fact_ids,
    ).fetchall()
    con.close()
    assert rows
    assert all(r["valid_at"] == t_ref for r in rows)


@pytest.mark.anyio
async def test_contradiction_supersedes_first_add(gateway):
    first = await gateway.add("proj", "The user lives in Paris.")
    assert first.fact_ids
    second = await gateway.add("proj", "The user lives in Berlin.")
    assert second.superseded_count >= 1
    assert set(first.fact_ids) & set(second.superseded_fact_ids)


@pytest.mark.anyio
async def test_search_returns_all_nine_hit_keys(gateway):
    await gateway.add("proj", "Carol enjoys hiking in the mountains.")
    res = await gateway.search("proj", "hiking")
    assert isinstance(res, SearchResult)
    assert res.hits
    hit = res.hits[0]
    for key in (
        "fact_id",
        "fact_text",
        "final_score",
        "relevance",
        "recency",
        "importance",
        "dense_rank",
        "sparse_rank",
        "rrf_score",
    ):
        assert key in hit


@pytest.mark.anyio
async def test_add_and_search_record_events(gateway, tmp_path):
    await gateway.add("proj", "Dave owns a bike.")
    await gateway.search("proj", "bike", origin="agent")
    log = EventLog(tmp_path)
    adds = log.list_events("proj", kind="add")
    searches = log.list_events("proj", kind="search")
    log.close()
    assert adds["total"] == 1
    assert searches["total"] == 1
    assert searches["items"][0]["payload"]["origin"] == "agent"


@pytest.mark.anyio
async def test_search_records_ui_origin(gateway, tmp_path):
    await gateway.add("proj", "Eve plays chess.")
    await gateway.search("proj", "chess", origin="ui")
    log = EventLog(tmp_path)
    searches = log.list_events("proj", kind="search")
    log.close()
    assert searches["items"][0]["payload"]["origin"] == "ui"


# ── sleep-time maintenance gateway methods (spec §8, §6.3) ────────────────────
def _stage_evict_proposal(root, namespace):
    """Stage one evict proposal directly on the namespace file — the review path's
    input. Dims match FakeEmbedder (768/256), the gateway's stub embedder."""
    import json

    import numpy as np
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


@pytest.mark.anyio
async def test_gateway_maintain_dry_run_writes_nothing(gateway, tmp_path):
    await gateway.add("proj", "I work at Acme.")
    report = await gateway.maintain("proj", apply=False)
    assert report["mode"] == "dry-run"
    import sqlite3

    con = sqlite3.connect(f"file:{tmp_path / 'proj.db'}?mode=ro", uri=True)
    runs = con.execute("SELECT COUNT(*) FROM maintenance_run").fetchone()[0]
    con.close()
    assert runs == 0


@pytest.mark.anyio
async def test_gateway_maintain_apply_records_run(gateway, tmp_path):
    await gateway.add("proj", "I work at Acme.")
    report = await gateway.maintain("proj", apply=True)
    assert report["mode"] == "apply"
    import sqlite3

    con = sqlite3.connect(f"file:{tmp_path / 'proj.db'}?mode=ro", uri=True)
    ok = con.execute(
        "SELECT COUNT(*) FROM maintenance_run WHERE status='ok'"
    ).fetchone()[0]
    con.close()
    assert ok == 1


@pytest.mark.anyio
async def test_gateway_review_queue_and_decide_roundtrip(gateway, tmp_path):
    pid, _fid = _stage_evict_proposal(tmp_path, "proj")
    groups = await gateway.review_queue("proj")
    ids = [p["id"] for g in groups for p in g["proposals"]]
    assert pid in ids
    result = await gateway.decide("proj", pid, "reject")
    assert result["outcome"] == "rejected"
    # Gone from the pending queue after the decision.
    groups2 = await gateway.review_queue("proj")
    assert all(p["id"] != pid for g in groups2 for p in g["proposals"])


@pytest.mark.anyio
async def test_gateway_promote_roundtrips(gateway, tmp_path):
    _pid, fid = _stage_evict_proposal(tmp_path, "proj")
    result = await gateway.promote("proj", fid)
    assert result["outcome"] == "promoted"
    assert result["tier"] == "hot"


@pytest.mark.anyio
async def test_gateway_maintenance_status_shape(gateway, tmp_path):
    """Status is a model-free ledger read shaped like the core server's."""
    _pid, _fid = _stage_evict_proposal(tmp_path, "proj")
    status = await gateway.maintenance_status("proj")
    assert status["namespace"] == "proj"
    assert status["runs"] >= 1
    assert status["pending_proposals"] == 1
    assert status["last_run"]["status"] == "ok"


@pytest.mark.anyio
async def test_gateway_maintenance_status_empty_namespace(gateway):
    status = await gateway.maintenance_status("never_written")
    assert status["runs"] == 0
    assert status["pending_proposals"] == 0
    assert status["last_run"] is None
