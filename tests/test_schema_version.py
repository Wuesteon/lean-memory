"""Namespace DBs carry a schema-version stamp so releases can migrate.

v0.1.0-0.1.2 shipped files with user_version=0 and no migration anchor; a schema
change would have had no way to tell an old-but-valid file from a foreign SQLite
database. Version 1 == the 0.1.x layout; version 2 == the sleep-time-maintenance
layout (adds fact.record_kind + the maintenance tables). A fresh DB is stamped at
the current version (2); older files migrate in place; a file stamped by a NEWER
release is never downgraded.

The genuine v1-format-file → v2 upgrade path (incl. the ALTER-idempotence reopen
trap) is pinned end-to-end in test_schema_migration.py against a checked-in
v1-format fixture DB. This module pins the stamp arithmetic.
"""

from lean_memory.store.sqlite_store import SqliteStore

CURRENT_SCHEMA_VERSION = 2


def _user_version(store: SqliteStore) -> int:
    return store._db.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_store_stamps_current_version(tmp_path):
    store = SqliteStore(tmp_path / "ns.db", dim=768)
    assert _user_version(store) == CURRENT_SCHEMA_VERSION
    store.close()


def test_migrated_file_reopens_at_current_version(tmp_path):
    """A DB already migrated to the current version reopens without re-running the
    non-idempotent migration DDL (the ALTER-idempotence trap)."""
    path = tmp_path / "ns.db"
    SqliteStore(path, dim=768).close()  # fresh → stamped current

    reopened = SqliteStore(path, dim=768)  # must NOT raise 'duplicate column name'
    assert _user_version(reopened) == CURRENT_SCHEMA_VERSION
    reopened.close()


def test_newer_version_is_not_downgraded(tmp_path):
    path = tmp_path / "ns.db"
    store = SqliteStore(path, dim=768)
    store._db.execute("PRAGMA user_version = 7")  # a future release's stamp
    store._db.commit()
    store.close()

    reopened = SqliteStore(path, dim=768)
    assert _user_version(reopened) == 7
    reopened.close()
