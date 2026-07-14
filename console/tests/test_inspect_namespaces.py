import shutil
import sqlite3

import pytest

from lean_memory_console import inspect_sql
from lean_memory_console.events import EventLog

from tests.fixtures.build_fixture import FIXTURE_DIR


def _copy_fixture(tmp_path):
    dst = tmp_path / "data_root"
    shutil.copytree(FIXTURE_DIR, dst)
    return dst


def test_open_ro_reads_fixture_db(tmp_path):
    root = _copy_fixture(tmp_path)
    conn = inspect_sql.open_ro(root / "proj-alpha.db")
    try:
        n = conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
        assert n > 0
    finally:
        conn.close()


def test_open_ro_reads_without_wal_sidecar(tmp_path):
    root = _copy_fixture(tmp_path)
    # delete any -wal/-shm sidecars — mode=ro must still read the base file
    for suffix in ("-wal", "-shm"):
        p = root / f"proj-alpha.db{suffix}"
        if p.exists():
            p.unlink()
    conn = inspect_sql.open_ro(root / "proj-alpha.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0] > 0
    finally:
        conn.close()


def test_open_ro_immutable_fallback_only_after_operationalerror(tmp_path, monkeypatch):
    root = _copy_fixture(tmp_path)
    calls = []
    real_connect = sqlite3.connect

    # Param named `database` (not `uri`) to match the real sqlite3.connect
    # signature: open_ro passes the connection string positionally AND uri=True
    # as a keyword, so a fake whose first param is `uri` would collide
    # ("multiple values for argument 'uri'") before the fallback path runs.
    def fake_connect(database, *a, **k):
        calls.append(database)
        if len(calls) == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    conn = inspect_sql.open_ro(root / "proj-alpha.db")
    try:
        # first attempt mode=ro (raised), second attempt immutable=1 (succeeded)
        assert "mode=ro" in calls[0]
        assert "immutable=1" in calls[1]
        assert conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0] > 0
    finally:
        conn.close()


def test_list_namespaces_skips_events_and_counts(tmp_path):
    root = _copy_fixture(tmp_path)
    log = EventLog(root)
    try:
        nss = inspect_sql.list_namespaces(root, log)
    finally:
        log.close()
    names = [n["name"] for n in nss]
    assert "_events" not in names
    assert set(names) == {"proj-alpha", "proj-beta"}
    alpha = next(n for n in nss if n["name"] == "proj-alpha")
    assert alpha["episodes"] == 2
    assert alpha["chains"] >= 1
    assert alpha["facts_retired"] >= 1
    assert alpha["facts_latest"] >= 1
    assert isinstance(alpha["top_predicates"], list)
    assert alpha["activity"]["adds"] >= 0  # activity envelope present


def test_fingerprints_match_expected():
    assert (
        inspect_sql.compute_engine_schema_fingerprint()
        == inspect_sql.EXPECTED_SCHEMA_FINGERPRINT
    )
    assert (
        inspect_sql.compute_sanitizer_fingerprint()
        == inspect_sql.EXPECTED_SANITIZER_FINGERPRINT
    )
