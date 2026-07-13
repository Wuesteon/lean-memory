import sqlite3

import pytest

from lean_memory_console.config import ConsoleConfig
from lean_memory_console.engine import (
    AddResult,
    EngineGateway,
    SearchResult,
    retry_busy,
)
from lean_memory_console.events import EventLog


def _config(tmp_path):
    return ConsoleConfig(data_root=tmp_path, mode="local", models="stub")


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
