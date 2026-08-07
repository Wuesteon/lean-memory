"""Namespace DBs carry a schema-version stamp so releases can migrate.

v0.1.0-0.1.2 shipped files with user_version=0 and no migration anchor; a schema
change would have had no way to tell an old-but-valid file from a foreign SQLite
database. Version 1 == the 0.1.x layout; version 2 == the sleep-time-maintenance
layout (adds fact.record_kind + the maintenance tables); version 3 == the entity
name-collation layout (adds entity.name_key + ix_entity_key, WP15). A fresh DB is
stamped at the current version (3); older files migrate in place; a file stamped
by a NEWER release is never downgraded.

The genuine old-format-file upgrade paths (v1→current and v2→current, incl. the
ALTER-idempotence reopen trap) are pinned end-to-end in test_schema_migration.py
against checked-in v1/v2-format fixture DBs. This module pins the stamp
arithmetic.
"""

from lean_memory.store.sqlite_store import SqliteStore

CURRENT_SCHEMA_VERSION = 3


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
