import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from phase2_ingest import parse_lme_timestamp, parse_locomo_timestamp


def test_parse_lme_timestamp():
    # 2023-04-10 23:07 UTC = 1681168020 s
    assert parse_lme_timestamp("2023/04/10 (Mon) 23:07") == 1_681_168_020_000


def test_parse_locomo_timestamp():
    # 2023-05-08 13:56 UTC = 1683554160 s
    assert parse_locomo_timestamp("1:56 pm on 8 May, 2023") == 1_683_554_160_000
