"""Reopening a namespace DB with a different embedder dim must fail loud and clear.

Regression for the v0.1.2 launch blocker: the vec0 table bakes the embedder dim into
its DDL at creation (FLOAT[{dim}]), and CREATE ... IF NOT EXISTS silently keeps the
old table on reopen. The realistic path — install [mcp] first (768-dim offline stub),
add facts, later install [models] (1024-dim Qwen) against the same data root —
surfaced deep in the pipeline as an opaque OperationalError / numpy shape mismatch.
The store must refuse the mismatch at open time with an actionable message.
"""

import numpy as np
import pytest

from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Episode, Fact, new_id, now_ms


def _add_one_fact(store: SqliteStore, dim: int) -> None:
    episode = Episode(namespace="ns", raw="seed", t_ref=0, source="test")
    store.add_episode(episode)
    from lean_memory.types import Entity

    entity = store.upsert_entity(Entity(namespace="ns", name="user", type=None))
    fact = Fact(
        id=new_id(), namespace="ns", subject_id=entity.id, predicate="works_at",
        object_literal="Acme", fact_text="I work at Acme.", valid_at=0,
        episode_id=episode.id, ingested_at=now_ms(), created_at=now_ms(),
    )
    store.add_fact(fact, np.ones(dim, dtype=np.float32), np.ones(256, dtype=np.float32))


def test_reopen_with_different_dim_raises_actionable_error(tmp_path):
    path = tmp_path / "ns.db"
    store = SqliteStore(path, dim=768)
    _add_one_fact(store, 768)
    store.close()

    with pytest.raises(ValueError) as exc:
        SqliteStore(path, dim=1024)
    msg = str(exc.value)
    assert "768" in msg and "1024" in msg  # both dims named
    assert str(path) in msg  # the file the user must act on
    assert "embedder" in msg.lower()


def test_coarse_dim_mismatch_names_the_coarse_dims(tmp_path):
    """A mismatch on the COARSE column must report that column's dims, not the
    (matching) full-embedding dim."""
    path = tmp_path / "ns.db"
    SqliteStore(path, dim=768, coarse_dim=256).close()

    with pytest.raises(ValueError) as exc:
        SqliteStore(path, dim=768, coarse_dim=128)
    msg = str(exc.value)
    assert "embedding_256" in msg
    assert "256" in msg and "128" in msg
    # The full-embedding dim MATCHED — naming it would misdirect the user.
    assert "768" not in msg


def test_reopen_with_same_dim_is_fine(tmp_path):
    path = tmp_path / "ns.db"
    store = SqliteStore(path, dim=768)
    _add_one_fact(store, 768)
    store.close()

    reopened = SqliteStore(path, dim=768)
    assert reopened.get_fact is not None  # opened cleanly
    hits = reopened.dense_search(
        np.ones(256, dtype=np.float32), np.ones(768, dtype=np.float32), k=1
    )
    assert len(hits) == 1
    reopened.close()
