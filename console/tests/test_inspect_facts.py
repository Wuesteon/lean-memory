import shutil

import pytest

from lean_memory_console import inspect_sql

from tests.fixtures.build_fixture import FIXTURE_DIR


@pytest.fixture()
def alpha_db(tmp_path):
    dst = tmp_path / "data_root"
    shutil.copytree(FIXTURE_DIR, dst)
    return dst / "proj-alpha.db"


def test_list_facts_latest_only_default(alpha_db):
    out = inspect_sql.list_facts(alpha_db)
    assert out["page"] == 1 and out["page_size"] == 50
    # default latest_only=True: no retired facts in the page
    assert out["total"] >= 1
    assert all(row["is_latest"] == 1 for row in out["items"])


def test_list_facts_includes_retired_when_flag_off(alpha_db):
    latest = inspect_sql.list_facts(alpha_db, latest_only=True)["total"]
    allf = inspect_sql.list_facts(alpha_db, latest_only=False)["total"]
    assert allf > latest  # retired chain member now visible


def test_list_facts_carries_subject_name(alpha_db):
    out = inspect_sql.list_facts(alpha_db, latest_only=False)
    # every fact row exposes the joined entity name as "subject"
    assert all("subject" in row for row in out["items"])
    assert any(row["subject"] == "Ada" for row in out["items"])


def test_list_facts_predicate_exact(alpha_db):
    allf = inspect_sql.list_facts(alpha_db, latest_only=False)
    pred = allf["items"][0]["predicate"]
    filtered = inspect_sql.list_facts(alpha_db, latest_only=False, predicate=pred)
    assert filtered["total"] >= 1
    assert all(r["predicate"] == pred for r in filtered["items"])


def test_list_facts_entity_case_insensitive(alpha_db):
    lower = inspect_sql.list_facts(alpha_db, latest_only=False, entity="ada")
    upper = inspect_sql.list_facts(alpha_db, latest_only=False, entity="ADA")
    assert lower["total"] == upper["total"] >= 2  # Ada has >=2 facts


def test_list_facts_min_salience(alpha_db):
    allf = inspect_sql.list_facts(alpha_db, latest_only=False)
    hi = max(r["salience"] for r in allf["items"])
    filtered = inspect_sql.list_facts(alpha_db, latest_only=False, min_salience=hi)
    assert filtered["total"] >= 1
    assert all(r["salience"] >= hi for r in filtered["items"])


def test_list_facts_q_fts_match(alpha_db):
    out = inspect_sql.list_facts(alpha_db, latest_only=False, q="Globex")
    assert out["total"] >= 1
    assert all("globex" in r["fact_text"].lower() for r in out["items"])


def test_list_facts_envelope_total_is_post_filter(alpha_db):
    allf = inspect_sql.list_facts(alpha_db, latest_only=False)
    one = inspect_sql.list_facts(alpha_db, latest_only=False, q="Globex")
    assert one["total"] <= allf["total"]


def test_get_fact_chain_oldest_to_newest(alpha_db):
    # find the retired fact (has superseded_by) then fetch the latest head
    allf = inspect_sql.list_facts(alpha_db, latest_only=False)
    head = next(r for r in allf["items"] if r["is_latest"] == 1 and r["subject"] == "Ada")
    got = inspect_sql.get_fact(alpha_db, head["id"])
    assert got is not None
    chain = got["chain"]
    assert len(chain) >= 2
    # oldest -> newest ordering: last is the latest, first is retired
    assert chain[-1]["is_latest"] == 1
    assert chain[0]["is_latest"] == 0
    assert got["episode"] is not None
    assert got["episode"]["id"] == got["episode_id"]


def test_get_fact_missing_returns_none(alpha_db):
    assert inspect_sql.get_fact(alpha_db, "does-not-exist") is None


def test_list_episodes_order_and_facts(alpha_db):
    out = inspect_sql.list_episodes(alpha_db)
    assert out["total"] == 2
    trefs = [e["t_ref"] for e in out["items"]]
    assert trefs == sorted(trefs, reverse=True)  # t_ref DESC


def test_get_episode_carries_its_facts(alpha_db):
    eps = inspect_sql.list_episodes(alpha_db)["items"]
    ep = inspect_sql.get_episode(alpha_db, eps[0]["id"])
    assert ep is not None
    assert "facts" in ep
    assert all(f["episode_id"] == ep["id"] for f in ep["facts"])


def test_list_entities_fact_count(alpha_db):
    out = inspect_sql.list_entities(alpha_db)
    assert out["total"] >= 1
    ada = next(e for e in out["items"] if e["name"] == "Ada")
    assert ada["fact_count"] >= 2
    # ordered fact_count DESC then name
    counts = [e["fact_count"] for e in out["items"]]
    assert counts == sorted(counts, reverse=True)
