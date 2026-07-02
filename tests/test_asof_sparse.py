"""Regression: the BM25 sparse arm must honor as_of (it previously ignored it,
leaking facts from outside the temporal window into fused results)."""

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.retrieve.rerank import IdentityReranker
from lean_memory.retrieve.retriever import Retriever
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact


def _seed_store(tmp_path):
    emb = FakeEmbedder()
    store = SqliteStore(tmp_path / "t.db", dim=emb.dim, coarse_dim=emb.coarse_dim)
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    store.add_episode(ep)
    ent = store.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    old = Fact(
        namespace="ns", subject_id=ent.id, predicate="works_at",
        fact_text="user works at zorbex", valid_at=1_000, episode_id=ep.id,
        valid_to=2_000, is_latest=0,
    )
    new = Fact(
        namespace="ns", subject_id=ent.id, predicate="works_at",
        fact_text="user works at quandril", valid_at=3_000, episode_id=ep.id,
    )
    for f in (old, new):
        full, coarse = emb.embed_with_coarse(f.fact_text)
        store.add_fact(f, full, coarse)
    return emb, store, old, new


def test_sparse_search_respects_as_of(tmp_path):
    _, store, old, new = _seed_store(tmp_path)
    hits = store.sparse_search("works", 5, is_latest_only=False, as_of=1_500)
    ids = [fid for fid, _ in hits]
    assert old.id in ids
    assert new.id not in ids  # valid_at=3000 > as_of — must not leak via BM25


def test_retriever_as_of_excludes_late_fact_end_to_end(tmp_path):
    emb, store, old, new = _seed_store(tmp_path)
    r = Retriever(store, emb, IdentityReranker())
    got = r.retrieve("works", 5, as_of=1_500, is_latest_only=False)
    got_ids = [x.fact.id for x in got]
    assert old.id in got_ids
    assert new.id not in got_ids
