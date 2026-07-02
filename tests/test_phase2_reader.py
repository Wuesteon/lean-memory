import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from lean_memory.types import Fact, RetrievedFact
from phase2_reader import EchoReader, READER_SYSTEM_PROMPT, render_user_prompt


def _hit(text: str, valid_at: int) -> RetrievedFact:
    f = Fact(namespace="ns", subject_id="e1", predicate="about",
             fact_text=text, valid_at=valid_at, episode_id="ep1")
    return RetrievedFact(fact=f, final_score=1.0, relevance=1.0, recency=0.0, importance=0.5)


def test_render_user_prompt_golden():
    hits = [_hit("user works at Quandril", 1_684_580_000_000),   # 2023-05-20
            _hit("user works at Zorbex", 1_681_168_020_000)]     # 2023-04-10
    got = render_user_prompt("Where does the user work now?", hits)
    assert got == (
        "- [2023-05-20] user works at Quandril\n"
        "- [2023-04-10] user works at Zorbex\n"
        "\nQuestion: Where does the user work now?"
    )


def test_render_user_prompt_empty():
    got = render_user_prompt("Q?", [])
    assert got == "(no facts retrieved)\n\nQuestion: Q?"


def test_echo_reader_returns_top1():
    assert EchoReader().answer("q", [_hit("alpha", 0), _hit("beta", 0)]) == "alpha"
    assert EchoReader().answer("q", []) == "I don't know"


def test_reader_prompt_frozen():
    assert READER_SYSTEM_PROMPT == (
        "Answer the question using only the provided facts. If none of the "
        "facts answer the question, say 'I don't know'."
    )
