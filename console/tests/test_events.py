import sqlite3
import threading

import pytest

from lean_memory_console.events import CAP, EventLog


def _score_payload():
    return {
        "query": "where does the user work?",
        "k": 5,
        "latest_only": True,
        "origin": "agent",
        "hits": [
            {
                "fact_id": "f1",
                "fact_text": "The user works at Acme.",
                "final_score": 0.82,
                "relevance": 0.9,
                "recency": 0.7,
                "importance": 0.5,
                "dense_rank": 1,
                "sparse_rank": 2,
                "rrf_score": 0.031,
            }
        ],
    }


def test_schema_created(tmp_path):
    log = EventLog(tmp_path)
    try:
        db = sqlite3.connect(tmp_path / "_events.db")
        cols = {r[1] for r in db.execute("PRAGMA table_info(event)").fetchall()}
        assert cols == {
            "id", "namespace", "ts", "kind", "duration_ms", "payload"
        }
        idx = {r[1] for r in db.execute("PRAGMA index_list(event)").fetchall()}
        assert "ix_event_ns_ts" in idx
        db.close()
    finally:
        log.close()


def test_record_and_list_roundtrip_decodes_payload(tmp_path):
    log = EventLog(tmp_path)
    try:
        payload = _score_payload()
        log.record("ns1", "search", 12.5, payload)
        out = log.list_events("ns1")
        assert out["total"] == 1
        assert out["page"] == 1 and out["page_size"] == 50
        item = out["items"][0]
        assert item["namespace"] == "ns1"
        assert item["kind"] == "search"
        assert item["duration_ms"] == 12.5
        assert item["payload"] == payload  # JSON round-trips, decoded to dict
        assert isinstance(item["ts"], int)
    finally:
        log.close()


def test_kind_filter(tmp_path):
    log = EventLog(tmp_path)
    try:
        log.record("ns1", "add", 1.0, {"fact_count": 2})
        log.record("ns1", "search", 2.0, _score_payload())
        assert log.list_events("ns1", kind="add")["total"] == 1
        assert log.list_events("ns1", kind="search")["total"] == 1
        assert log.list_events("ns1")["total"] == 2
    finally:
        log.close()


def test_list_ordered_ts_desc_then_id_desc(tmp_path):
    log = EventLog(tmp_path)
    try:
        for i in range(3):
            log.record("ns1", "add", float(i), {"n": i})
        items = log.list_events("ns1")["items"]
        # newest first: id DESC breaks ties on identical ts
        ids = [it["id"] for it in items]
        assert ids == sorted(ids, reverse=True)
    finally:
        log.close()


def test_activity_summary_excludes_ui_origin(tmp_path):
    log = EventLog(tmp_path)
    try:
        log.record("ns1", "add", 1.0, {"source": "user"})
        log.record("ns1", "search", 1.0, {"origin": "agent"})
        log.record("ns1", "search", 1.0, {"origin": "ui"})  # excluded
        summ = log.activity_summary("ns1")
        assert summ["adds"] == 1
        assert summ["searches"] == 1  # the ui search is excluded
        assert isinstance(summ["earliest_ts"], int)
    finally:
        log.close()


def test_activity_summary_empty_earliest_none(tmp_path):
    log = EventLog(tmp_path)
    try:
        summ = log.activity_summary("nobody")
        assert summ == {"adds": 0, "searches": 0, "earliest_ts": None}
    finally:
        log.close()


def test_retention_prunes_to_cap_keeping_newest(tmp_path):
    log = EventLog(tmp_path)
    try:
        for i in range(CAP + 1):  # 10_001 events
            log.record("ns1", "add", float(i), {"n": i})
        out = log.list_events("ns1", page_size=1)
        assert out["total"] == CAP  # pruned to exactly 10_000
        # the surviving newest carries the last payload
        assert out["items"][0]["payload"]["n"] == CAP
        # the oldest (n == 0) is gone
        db = sqlite3.connect(tmp_path / "_events.db")
        gone = db.execute(
            "SELECT COUNT(*) FROM event WHERE namespace=? AND payload LIKE ?",
            ("ns1", '%"n": 0%'),
        ).fetchone()[0]
        db.close()
        assert gone == 0
    finally:
        log.close()


def test_record_never_raises(tmp_path, caplog):
    log = EventLog(tmp_path)
    try:
        # Force the write path to throw; record must swallow + log + return None.
        # NB: sqlite3.Connection.execute is a read-only slot on modern CPython, so
        # monkeypatch.setattr(log._db, "execute", ...) is impossible — we swap the
        # whole _db for a proxy whose execute() raises instead.
        class _BoomDB:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("disk I/O error")

            def commit(self):
                ...

            def close(self):
                ...

        log._db = _BoomDB()
        import logging

        with caplog.at_level(logging.WARNING, logger="lean_memory_console.events"):
            assert log.record("ns1", "add", 1.0, {"x": 1}) is None
        assert any("lean_memory_console.events" == r.name for r in caplog.records)
    finally:
        # Restore a real connection so close() releases the file cleanly.
        log._db = sqlite3.connect(str(log.path))
        log.close()


def test_concurrent_records_lose_nothing(tmp_path):
    log = EventLog(tmp_path)
    try:
        n_threads, per = 4, 50

        def worker(t):
            for i in range(per):
                log.record("ns1", "add", float(i), {"t": t, "i": i})

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert log.list_events("ns1", page_size=1)["total"] == n_threads * per
    finally:
        log.close()
