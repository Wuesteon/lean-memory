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
