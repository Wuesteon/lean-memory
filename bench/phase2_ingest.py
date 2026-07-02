"""Phase 2 ingest adapters: LongMemEval + LoCoMo → lean-memory public API.

Maps each benchmark's conversation structure onto exactly two calls:
    mem.add(namespace, text, t_ref=<epoch_ms>, source=<str>)
No private engine paths. See docs/superpowers/specs/2026-07-02-phase2-eval-harness-design.md.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dateutil import parser as dateparser  # noqa: E402  (core dep python-dateutil)


class DatasetError(ValueError):
    """A dataset file failed validation — abort loudly, never mis-score."""


@dataclass(frozen=True)
class Turn:
    text: str
    t_ref: int  # epoch-ms world time
    source: str


@dataclass(frozen=True)
class Question:
    question_id: str
    question_type: str
    question: str
    gold: str
    question_date: str = ""
    is_abstention: bool = False
    category: Optional[int] = None


@dataclass
class IngestUnit:
    namespace: str
    turns: list[Turn] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)


# ── timestamps ──

_PAREN = re.compile(r"\s*\([^)]*\)")


def parse_lme_timestamp(s: str) -> int:
    """LongMemEval session/question dates: '2023/04/10 (Mon) 23:07' → epoch-ms UTC."""
    clean = _PAREN.sub("", s).strip()
    dt = datetime.strptime(clean, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_locomo_timestamp(s: str) -> int:
    """LoCoMo session dates: '1:56 pm on 8 May, 2023' → epoch-ms UTC."""
    dt = dateparser.parse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ── dataset registry + download ──

DATASETS = {
    "locomo10": {
        "url": "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
        "filename": "locomo10.json",
        "license": "CC BY-NC 4.0 (eval use only, not redistributed)",
    },
    "lme_oracle": {
        "url": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json",
        "filename": "longmemeval_oracle.json",
        "license": "MIT",
    },
    "lme_s": {
        "url": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json",
        "filename": "longmemeval_s_cleaned.json",
        "license": "MIT",
    },
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_dataset(name: str, data_dir: Path) -> tuple[Path, str]:
    """Download-if-missing; record sha256 on first download; verify thereafter."""
    spec = DATASETS[name]
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / spec["filename"]
    sha_path = data_dir / (spec["filename"] + ".sha256")
    if not path.exists():
        print(f"downloading {name} ← {spec['url']}")
        urllib.request.urlretrieve(spec["url"], path)  # noqa: S310
    sha = _sha256_file(path)
    if sha_path.exists():
        pinned = sha_path.read_text().strip()
        if pinned != sha:
            raise DatasetError(
                f"{path.name}: sha256 {sha[:16]}… != pinned {pinned[:16]}… "
                "(dataset changed upstream — delete both files to re-pin deliberately)"
            )
    else:
        sha_path.write_text(sha)
    return path, sha


# ── LongMemEval ──

LME_TOTAL_QUESTIONS = 500
LME_KU_QUESTIONS = 78
_TURN_STEP_MS = 1_000  # +1s per turn inside a session: supersession order is defined


def _lme_sessions(entry: dict) -> list[tuple[str, Optional[str], list[dict]]]:
    """Normalize both on-disk shapes → [(session_id, date_str|None, turns)]."""
    ids = entry.get("haystack_session_ids") or []
    dates = entry.get("haystack_dates") or []
    out = []
    for i, item in enumerate(entry["haystack_sessions"]):
        if isinstance(item, list):  # _s shape: the entry IS the turn list
            sid = ids[i] if i < len(ids) else f"{entry['question_id']}_s{i}"
            turns = item
        elif isinstance(item, dict) and isinstance(item.get("turns"), list):
            sid = item.get("session_id") or f"{entry['question_id']}_s{i}"
            turns = item["turns"]
        else:
            raise DatasetError(f"{entry['question_id']}: malformed haystack session #{i}")
        out.append((sid, dates[i] if i < len(dates) else None, turns))
    return out


def load_longmemeval(path: Path, slice: str = "all", expect_counts: bool = False) -> list[IngestUnit]:
    data = json.loads(Path(path).read_text())
    if expect_counts and len(data) != LME_TOTAL_QUESTIONS:
        raise DatasetError(f"expected {LME_TOTAL_QUESTIONS} LME questions, got {len(data)}")
    units: list[IngestUnit] = []
    for entry in data:
        q = Question(
            question_id=entry["question_id"],
            question_type=entry["question_type"],
            question=entry["question"],
            gold=str(entry["answer"]),
            question_date=entry.get("question_date", ""),
            is_abstention=entry["question_id"].endswith("_abs"),
        )
        if slice == "ku" and q.question_type != "knowledge-update":
            continue
        sessions = []
        prev_t0: Optional[int] = None
        for sid, date, sturns in _lme_sessions(entry):
            if date:
                t0 = parse_lme_timestamp(date)
            elif prev_t0 is not None:
                t0 = prev_t0 + 3_600_000  # dateless session: 1h after the previous
            elif q.question_date:
                t0 = parse_lme_timestamp(q.question_date) - 86_400_000 * len(entry["haystack_sessions"])
            else:
                raise DatasetError(f"{q.question_id}: session {sid} has no usable date")
            prev_t0 = t0
            sessions.append((t0, sturns))
        # oracle variant is NOT time-sorted; _s is. Sorting is a no-op for _s.
        sessions.sort(key=lambda x: x[0])
        turns = [
            Turn(text=t["content"], t_ref=t0 + j * _TURN_STEP_MS, source=t["role"])
            for t0, sturns in sessions
            for j, t in enumerate(sturns)
        ]
        units.append(IngestUnit(namespace=q.question_id, turns=turns, questions=[q]))
    if expect_counts and slice == "ku" and len(units) != LME_KU_QUESTIONS:
        raise DatasetError(f"expected {LME_KU_QUESTIONS} KU questions, got {len(units)}")
    return units
