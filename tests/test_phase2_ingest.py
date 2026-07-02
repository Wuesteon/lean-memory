import sys
from pathlib import Path

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
