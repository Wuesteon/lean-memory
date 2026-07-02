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


# ── LoCoMo ──

LOCOMO_CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}
LOCOMO_CATEGORY_ANCHORS = {1: 282, 2: 321, 3: 96, 4: 841, 5: 446}
LOCOMO_CONVS = 10
_SESSION_KEY = re.compile(r"^session_(\d+)$")


def load_locomo(path: Path, slice: str = "all", expect_counts: bool = False) -> list[IngestUnit]:
    data = json.loads(Path(path).read_text())
    if expect_counts:
        if len(data) != LOCOMO_CONVS:
            raise DatasetError(f"expected {LOCOMO_CONVS} conversations, got {len(data)}")
        counts: dict[int, int] = {}
        for conv in data:
            for qa in conv["qa"]:
                counts[qa["category"]] = counts.get(qa["category"], 0) + 1
        if counts != LOCOMO_CATEGORY_ANCHORS:
            raise DatasetError(f"category counts {counts} != anchors {LOCOMO_CATEGORY_ANCHORS}")
    wanted = {2} if slice == "temporal" else {1, 2, 3, 4}
    units: list[IngestUnit] = []
    for conv in data:
        c = conv["conversation"]
        sessions = sorted(
            (int(m.group(1)) for key in c if (m := _SESSION_KEY.match(key))),
        )
        turns: list[Turn] = []
        for n in sessions:
            date_key = f"session_{n}_date_time"
            if date_key not in c:
                raise DatasetError(f"{conv['sample_id']}: missing {date_key}")
            t0 = parse_locomo_timestamp(c[date_key])
            for j, d in enumerate(c[f"session_{n}"]):
                text = f"{d['speaker']}: {d['text']}"
                if d.get("blip_caption"):
                    text += f"\n{d['speaker']} shared a photo: {d['blip_caption']}"
                turns.append(Turn(text=text, t_ref=t0 + j * _TURN_STEP_MS, source=d["speaker"]))
        questions = [
            Question(
                question_id=f"{conv['sample_id']}_q{i:03d}",
                question_type=LOCOMO_CATEGORY_NAMES[qa["category"]],
                question=qa["question"],
                gold=str(qa.get("answer", qa.get("adversarial_answer", ""))),
                category=qa["category"],
            )
            for i, qa in enumerate(conv["qa"])
            if qa["category"] in wanted
        ]
        units.append(IngestUnit(namespace=conv["sample_id"], turns=turns, questions=questions))
    return units


# ── memory factory + cached ingest ──

import sqlite3  # noqa: E402
import time  # noqa: E402

from lean_memory.memory import Memory  # noqa: E402

DEFAULT_EMBEDDER = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_TYPER_MODEL = "qwen2.5:3b"
DEFAULT_GENERATOR_MODEL = "fastino/gliner2-base-v1"
DEFAULT_RERANKER_MODEL = "cross-encoder/ettin-reranker-32m-v1"

# Remote typer host (e.g. an HF Space running the SAME ollama runtime + blob).
# Set PHASE2_OLLAMA_HOST to offload the typing pass; PHASE2_OLLAMA_TOKEN adds a
# bearer header for private hosts. Recorded in the manifest engine dict so a
# cache ingested against one host can never silently continue on another.
import os  # noqa: E402


def typer_host() -> Optional[str]:
    return os.environ.get("PHASE2_OLLAMA_HOST") or None


def _typer_headers() -> Optional[dict]:
    tok = os.environ.get("PHASE2_OLLAMA_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else None


def build_typer():
    from lean_memory.extract.llm_typer import OllamaTyper

    host = typer_host()
    if not host:
        return OllamaTyper(DEFAULT_TYPER_MODEL)

    class RemoteOllamaTyper(OllamaTyper):
        """Same model, same constrained decode — only transport differs."""

        def _get_client(self):
            if self._client is None:
                import ollama

                self._client = ollama.Client(host=self.host, headers=_typer_headers())
            return self._client

    return RemoteOllamaTyper(DEFAULT_TYPER_MODEL, host=host)


def build_memory(root: Path, *, real: bool, embedder_name: str = DEFAULT_EMBEDDER) -> Memory:
    """Offline: every backend is the deterministic stub (plumbing only).
    Real: the full production stack; router/contradiction keep frozen defaults."""
    if not real:
        return Memory(root=root)
    from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder
    from lean_memory.extract.gliner_extractor import Gliner2Generator
    from lean_memory.retrieve.rerank import CrossEncoderReranker

    return Memory(
        root=root,
        embedder=SentenceTransformerEmbedder(embedder_name),
        reranker=CrossEncoderReranker(),
        generator=Gliner2Generator(),
        typer=build_typer(),
    )


def typer_digest(client=None) -> str:
    """Manifest digest of the typer model as served — the byte-level pin."""
    import ollama

    client = client or (
        ollama.Client(host=typer_host(), headers=_typer_headers()) if typer_host() else ollama
    )
    for m in client.list().models:
        if m.model == DEFAULT_TYPER_MODEL:
            return m.digest
    raise KeyError(f"{DEFAULT_TYPER_MODEL} not present on typer host")


def preflight_real() -> None:
    """Abort with guidance (never mid-ingest) when the typer backend is down."""
    from bet2_ablation import BackendUnavailable

    host = typer_host()
    try:
        typer_digest()
    except Exception as exc:  # noqa: BLE001 — any transport failure = unavailable
        where = host or "local ollama"
        raise BackendUnavailable(
            f"typer backend unreachable ({where}): {exc}\n"
            f"  Local:  ollama serve  &&  ollama pull {DEFAULT_TYPER_MODEL}\n"
            f"  Remote: check the host is up and PHASE2_OLLAMA_TOKEN is valid"
        ) from exc


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = min(len(ys) - 1, int(round(p * (len(ys) - 1))))
    return ys[i]


def _count_supersessions(db_path: Path) -> int:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT COUNT(*) FROM fact WHERE superseded_by IS NOT NULL"
        ).fetchone()[0]


def ingest_units(
    units: list[IngestUnit],
    cache_dir: Path,
    *,
    real: bool,
    embedder_name: str = DEFAULT_EMBEDDER,
    limit: Optional[int] = None,
) -> dict:
    from lean_memory.extract.llm_typer import TyperError
    from bet2_ablation import BackendUnavailable
    from lean_memory.memory import _SAFE_NS

    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "manifest.json"
    engine = {"real": real, "embedder": embedder_name if real else "FakeEmbedder",
              "generator": DEFAULT_GENERATOR_MODEL if real else "StubCandidateGenerator",
              "typer": DEFAULT_TYPER_MODEL if real else "StubTyper"}
    if real:
        preflight_real()  # also validates the typer host before we pin it below
        engine["typer_host"] = typer_host() or "local"
        engine["typer_digest"] = typer_digest()
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "engine": engine, "namespaces": {}}
    if manifest["engine"] != engine:
        raise DatasetError(
            f"cache {cache_dir} was built with engine {manifest['engine']}, requested {engine} — use a fresh cache dir")
    mem = build_memory(cache_dir, real=real, embedder_name=embedder_name)
    try:
        todo = [u for u in units if not manifest["namespaces"].get(u.namespace, {}).get("done")]
        for u in todo[: limit if limit is not None else len(todo)]:
            base = dict(mem.router.cumulative_stats)
            add_ms: list[float] = []
            facts = 0
            for turn in u.turns:
                t0 = time.perf_counter()
                try:
                    facts += len(mem.add(u.namespace, turn.text, t_ref=turn.t_ref, source=turn.source))
                except TyperError as exc:
                    raise BackendUnavailable(
                        f"typer died mid-ingest on {u.namespace}: {exc}\n"
                        f"  Restart ollama and re-run — completed namespaces are cached."
                    ) from exc
                add_ms.append((time.perf_counter() - t0) * 1000)
            cur = mem.router.cumulative_stats
            db_path = cache_dir / f"{_SAFE_NS.sub('_', u.namespace) or 'default'}.db"
            manifest["namespaces"][u.namespace] = {
                "done": True,
                "turns": len(u.turns),
                "facts": facts,
                "supersessions": _count_supersessions(db_path),
                "escalated": cur.get("escalated", 0) - base.get("escalated", 0),
                "seen": cur.get("seen", 0) - base.get("seen", 0),
                "add_ms_p50": round(_percentile(add_ms, 0.50), 2),
                "add_ms_p95": round(_percentile(add_ms, 0.95), 2),
            }
            tmp = manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(manifest, indent=1))
            tmp.replace(manifest_path)
            print(f"ingested {u.namespace}: {len(u.turns)} turns → {facts} facts")
    finally:
        mem.close()
    return manifest


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Download + validate Phase 2 datasets.")
    ap.add_argument("--data-dir", default=str(_BENCH / ".phase2_cache" / "data"))
    ap.add_argument("--datasets", default="locomo10,lme_oracle,lme_s")
    args = ap.parse_args()
    for name in args.datasets.split(","):
        path, sha = ensure_dataset(name, Path(args.data_dir))
        if name == "locomo10":
            n = sum(len(u.questions) for u in load_locomo(path, expect_counts=True))
            print(f"{name}: sha256 {sha[:16]}  OK  ({n} scorable questions)")
        else:
            ku = load_longmemeval(path, slice="ku", expect_counts=True)
            print(f"{name}: sha256 {sha[:16]}  OK  ({len(ku)} KU questions)")
