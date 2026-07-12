"""FTS5 query sanitization: arbitrary natural language must never raise.

Regression for the v0.1.2 major: `_fts_query` joined bare terms with ' OR ', but
FTS5 treats the bare uppercase tokens AND/OR/NOT/NEAR as operators — so a query
like 'coffee AND tea' produced the malformed 'coffee OR AND OR tea' and raised
sqlite3.OperationalError straight through Memory.search and the MCP tool. Quoting
every term as an FTS5 string literal makes operator words inert, and the sparse
arm additionally degrades to [] on any residual FTS syntax error (the dense arm
still serves the query).
"""

import pytest

from lean_memory import Memory
from lean_memory.store import sqlite_store
from lean_memory.store.sqlite_store import _fts_query


def test_fts_query_quotes_every_term():
    q = _fts_query("coffee AND tea")
    assert q == '"coffee" OR "AND" OR "tea"'


def test_fts_query_empty_stays_safe():
    assert _fts_query("") == '""'
    assert _fts_query("!!! ...") == '""'


@pytest.mark.parametrize(
    "query",
    [
        "coffee AND tea",
        "NEAR AND OR NOT",
        "what is NEAR the office",
        'do I like "coffee" OR tea?',
    ],
)
def test_search_with_operator_words_does_not_raise(tmp_path, query):
    mem = Memory(root=tmp_path)
    mem.add("ns", "I like coffee and tea.", t_ref=0)
    hits = mem.search("ns", query, k=3)
    assert isinstance(hits, list)  # no OperationalError end-to-end
    mem.close()


def test_operator_words_still_match_as_plain_terms(tmp_path):
    mem = Memory(root=tmp_path)
    mem.add("ns", "I like coffee and tea.", t_ref=0)
    hits = mem.search("ns", "coffee AND tea", k=3)
    assert hits, "quoted terms should still match the stored fact"
    mem.close()


def test_sparse_search_propagates_non_syntax_operational_errors(tmp_path):
    """Only FTS syntax errors degrade to []; a real store error like 'database
    is locked' must propagate, not silently drop the lexical arm."""
    import sqlite3

    mem = Memory(root=tmp_path)
    mem.add("ns", "I like coffee and tea.", t_ref=0)
    store = mem._store("ns")

    class LockedDb:
        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

    store._db = LockedDb()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        store.sparse_search("coffee", k=3)


def test_sparse_search_degrades_on_malformed_fts_query(tmp_path, monkeypatch):
    """Defense in depth: if a malformed query ever reaches FTS5 again, the sparse
    arm returns [] instead of blowing up the whole search."""
    mem = Memory(root=tmp_path)
    mem.add("ns", "I like coffee and tea.", t_ref=0)
    store = mem._store("ns")
    monkeypatch.setattr(sqlite_store, "_fts_query", lambda text: "coffee OR AND OR tea")
    assert store.sparse_search("coffee AND tea", k=3) == []
    mem.close()
