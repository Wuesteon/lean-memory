import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from phase2_ingest import parse_lme_timestamp, parse_locomo_timestamp
from phase2_ingest import DatasetError, load_longmemeval

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "phase2"


def test_parse_lme_timestamp():
    # 2023-04-10 23:07 UTC = 1681168020 s
    assert parse_lme_timestamp("2023/04/10 (Mon) 23:07") == 1_681_168_020_000


def test_parse_locomo_timestamp():
    # 2023-05-08 13:56 UTC = 1683554160 s
    assert parse_locomo_timestamp("1:56 pm on 8 May, 2023") == 1_683_554_160_000


def test_lme_s_shape_loads_units_with_ordered_trefs():
    units = load_longmemeval(FIXTURES / "lme_s_mini.json")
    assert [u.namespace for u in units] == ["ku_001", "ms_001_abs"]
    u = units[0]
    assert len(u.turns) == 3
    t1, t2, t3 = u.turns
    assert t1.t_ref == 1_681_168_020_000            # session 1 start
    assert t2.t_ref == 1_681_168_020_000 + 1_000    # +1s per turn
    assert t3.t_ref == parse_lme_timestamp("2023/05/20 (Sat) 11:30")
    assert (t1.source, t2.source, t3.source) == ("user", "assistant", "user")
    assert u.questions[0].gold == "Quandril"
    assert u.questions[0].is_abstention is False
    assert units[1].questions[0].is_abstention is True


def test_lme_oracle_shape_matches_s_shape():
    s = load_longmemeval(FIXTURES / "lme_s_mini.json", slice="ku")
    o = load_longmemeval(FIXTURES / "lme_oracle_mini.json", slice="ku")
    assert [t.text for t in s[0].turns] == [t.text for t in o[0].turns]
    assert [t.t_ref for t in s[0].turns] == [t.t_ref for t in o[0].turns]


def test_lme_ku_slice_filters():
    units = load_longmemeval(FIXTURES / "lme_s_mini.json", slice="ku")
    assert [u.namespace for u in units] == ["ku_001"]


def test_lme_expect_counts_aborts_on_fixture():
    import pytest
    with pytest.raises(DatasetError):
        load_longmemeval(FIXTURES / "lme_s_mini.json", expect_counts=True)


from phase2_ingest import load_locomo, parse_locomo_timestamp


def test_locomo_loads_conversation_unit():
    units = load_locomo(FIXTURES / "locomo_mini.json")
    assert len(units) == 1
    u = units[0]
    assert u.namespace == "conv-mini"
    assert len(u.turns) == 3
    assert u.turns[0].text == "Caroline: I'm thinking about moving out of Portland."
    assert u.turns[0].source == "Caroline"
    assert u.turns[0].t_ref == parse_locomo_timestamp("1:56 pm on 8 May, 2023")
    assert u.turns[1].t_ref == u.turns[0].t_ref + 1_000
    # image turn carries the caption on its own line
    assert u.turns[2].text == (
        "Caroline: I finally moved to Seattle last week!\n"
        "Caroline shared a photo: a moving truck"
    )
    # slice "all" keeps categories 1-4 only (adversarial excluded)
    assert [q.category for q in u.questions] == [2, 4]
    assert u.questions[0].question_id == "conv-mini_q000"
    assert u.questions[0].question_type == "temporal"


def test_locomo_temporal_slice():
    units = load_locomo(FIXTURES / "locomo_mini.json", slice="temporal")
    assert [q.category for q in units[0].questions] == [2]


def test_locomo_expect_counts_aborts_on_fixture():
    import pytest
    with pytest.raises(DatasetError):
        load_locomo(FIXTURES / "locomo_mini.json", expect_counts=True)


import json


def test_offline_ingest_cache_and_resume(tmp_path):
    from phase2_ingest import build_memory, ingest_units, load_longmemeval

    units = load_longmemeval(FIXTURES / "lme_s_mini.json")
    cache = tmp_path / "cache"
    m1 = ingest_units(units, cache, real=False)
    assert set(m1["namespaces"]) == {"ku_001", "ms_001_abs"}
    assert all(v["done"] for v in m1["namespaces"].values())
    assert (cache / "manifest.json").exists()
    assert (cache / "ku_001.db").exists()
    # resume: second call must not re-ingest (facts counters unchanged)
    m2 = ingest_units(units, cache, real=False)
    assert m2["namespaces"] == m1["namespaces"]
    # searchable through the public API
    mem = build_memory(cache, real=False)
    hits = mem.search("ku_001", "where does the user work", k=3)
    mem.close()
    assert isinstance(hits, list)


def test_remote_typer_env_wiring(monkeypatch):
    # _get_client() below does `import ollama` (the optional [llm] extra); skip
    # cleanly on a bare `.[dev]` install instead of erroring. CI installs the
    # extras that make this run.
    pytest.importorskip("ollama", reason="optional [llm] extra not installed")

    from phase2_ingest import build_typer

    monkeypatch.delenv("PHASE2_OLLAMA_HOST", raising=False)
    local = build_typer()
    assert type(local).__name__ == "OllamaTyper" and local.host is None

    monkeypatch.setenv("PHASE2_OLLAMA_HOST", "https://example.hf.space")
    monkeypatch.setenv("PHASE2_OLLAMA_TOKEN", "hf_test")
    remote = build_typer()
    assert remote.host == "https://example.hf.space"
    client = remote._get_client()  # constructs, does not connect
    assert client._client.headers["authorization"] == "Bearer hf_test"
    assert remote.model == "qwen2.5:3b"


def test_resume_reingest_does_not_duplicate(tmp_path):
    """Crash-resume idempotency: a namespace not marked done must restart from a
    clean DB, not append duplicate episodes/facts into the partial one."""
    import json
    import sqlite3

    from phase2_ingest import build_memory, ingest_units, load_longmemeval

    units = load_longmemeval(FIXTURES / "lme_s_mini.json", slice="ku")
    cache = tmp_path / "cache"
    ingest_units(units, cache, real=False)
    manifest_path = cache / "manifest.json"
    m = json.loads(manifest_path.read_text())
    del m["namespaces"]["ku_001"]  # simulate a crash mid-namespace
    manifest_path.write_text(json.dumps(m))

    ingest_units(units, cache, real=False)  # resume
    with sqlite3.connect(f"file:{cache / 'ku_001.db'}?mode=ro", uri=True) as db:
        episodes = db.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
    assert episodes == len(units[0].turns)  # not doubled


def test_remote_typer_repulls_on_model_not_found(monkeypatch):
    """The remote host may restart and lose its ephemeral model: the typer must
    re-pull once and retry, not kill the whole shard."""
    from lean_memory.extract.llm_typer import OllamaTyper, TyperError

    monkeypatch.setenv("PHASE2_OLLAMA_HOST", "https://example.hf.space")
    from phase2_ingest import build_typer

    remote = build_typer()
    calls = {"type": 0, "pull": 0}

    def fake_parent(self, *a, **k):
        calls["type"] += 1
        if calls["type"] == 1:
            raise TyperError("Ollama typing call failed: model 'qwen2.5:3b' not found (status code: 404)")
        return ["ok"]

    class FakeClient:
        def pull(self, model):
            calls["pull"] += 1

    monkeypatch.setattr(OllamaTyper, "type_candidates", fake_parent)
    remote._client = FakeClient()
    assert remote.type_candidates("ep", [], known_entities=[]) == ["ok"]
    assert calls == {"type": 2, "pull": 1}


def test_remote_typer_retries_transient_5xx(monkeypatch):
    """A flaky proxy 500 must not kill a shard: retry with backoff, then raise."""
    from lean_memory.extract.llm_typer import OllamaTyper, TyperError

    monkeypatch.setenv("PHASE2_OLLAMA_HOST", "https://example.hf.space")
    from phase2_ingest import build_typer

    remote = build_typer()
    calls = {"type": 0, "sleep": []}
    monkeypatch.setattr("time.sleep", lambda s: calls["sleep"].append(s))

    def fake_parent(self, *a, **k):
        calls["type"] += 1
        if calls["type"] < 3:
            raise TyperError("Ollama typing call failed: <html>500</html> (status code: 500)")
        return ["ok"]

    monkeypatch.setattr(OllamaTyper, "type_candidates", fake_parent)
    assert remote.type_candidates("ep", [], known_entities=[]) == ["ok"]
    assert calls["type"] == 3 and len(calls["sleep"]) == 2

    calls["type"] = 100  # now: permanent failure path — must raise after retries
    def always_fail(self, *a, **k):
        raise TyperError("Ollama typing call failed: boom (status code: 502)")
    monkeypatch.setattr(OllamaTyper, "type_candidates", always_fail)
    import pytest
    with pytest.raises(TyperError):
        remote.type_candidates("ep", [], known_entities=[])
