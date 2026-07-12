"""Namespace DBs carry a schema-version stamp so future releases can migrate.

v0.1.0-0.1.2 shipped files with user_version=0 and no migration anchor; a 0.2.0
schema change would have had no way to tell an old-but-valid file from a foreign
SQLite database. Version 1 == the 0.1.x layout, and pre-stamp files are upgraded
to 1 in place (their schema is identical). A file stamped by a NEWER release is
never downgraded.
"""

from lean_memory.store.sqlite_store import SqliteStore


def _user_version(store: SqliteStore) -> int:
    return store._db.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_store_stamps_user_version_1(tmp_path):
    store = SqliteStore(tmp_path / "ns.db", dim=768)
    assert _user_version(store) == 1
    store.close()


def test_prestamp_file_upgraded_in_place(tmp_path):
    path = tmp_path / "ns.db"
    store = SqliteStore(path, dim=768)
    store._db.execute("PRAGMA user_version = 0")  # simulate a 0.1.0-0.1.2 file
    store._db.commit()
    store.close()

    reopened = SqliteStore(path, dim=768)
    assert _user_version(reopened) == 1
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
