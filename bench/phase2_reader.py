"""Phase 2 reader — an instrument, not a place to squeeze points.

Frozen prompts; temperature 0; no chain-of-thought; content never retried.
Offline EchoReader proves plumbing; OpenRouterReader produces the real number.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bet2_ablation import BackendUnavailable  # noqa: E402

READER_SYSTEM_PROMPT = (
    "Answer the question using only the provided facts. If none of the "
    "facts answer the question, say 'I don't know'."
)
FC_SYSTEM_PROMPT = (
    "Answer the question using only the provided conversation transcript. "
    "If the transcript does not answer the question, say 'I don't know'."
)
DEFAULT_BACKBONE = "openai/gpt-4.1-mini"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_MAX_TRANSPORT_RETRIES = 5


def _iso_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def render_user_prompt(question: str, hits: list) -> str:
    """Facts block, ranked order, each line dated from the monotemporal spine."""
    if hits:
        block = "\n".join(f"- [{_iso_date(h.fact.valid_at)}] {h.fact.fact_text}" for h in hits)
    else:
        block = "(no facts retrieved)"
    return f"{block}\n\nQuestion: {question}"


def unit_transcript(unit) -> str:
    """Full-context baseline input. LoCoMo turn text already carries the
    speaker prefix; LME text does not, so prefix with the role there."""
    lines = []
    for t in unit.turns:
        body = t.text if t.text.startswith(f"{t.source}:") else f"{t.source}: {t.text}"
        lines.append(f"[{_iso_date(t.t_ref)}] {body}")
    return "\n".join(lines)


def openrouter_chat(model: str, messages: list[dict], *, temperature: float = 0.0,
                    max_tokens: int = 256) -> str:
    """One content attempt; transport errors retry with backoff then abort."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise BackendUnavailable(
            "OPENROUTER_API_KEY is not set.\n  export OPENROUTER_API_KEY=sk-or-…  and re-run."
        )
    import openai
    from openai import OpenAI

    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)
    last: Exception | None = None
    for attempt in range(_MAX_TRANSPORT_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content
            if content is None:
                raise BackendUnavailable(f"{model} returned empty content")
            return content
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as exc:
            last = exc
            if attempt < _MAX_TRANSPORT_RETRIES - 1:
                time.sleep(2**attempt)
    raise BackendUnavailable(f"OpenRouter unreachable after {_MAX_TRANSPORT_RETRIES} tries: {last}")


class EchoReader:
    """Offline stub: deterministic, semantically meaningless — plumbing only."""

    model = "echo"

    def answer(self, question: str, hits: list) -> str:
        return hits[0].fact.fact_text if hits else "I don't know"

    def answer_full_context(self, question: str, transcript: str) -> str:
        return transcript.splitlines()[0] if transcript else "I don't know"


class OpenRouterReader:
    def __init__(self, model: str = DEFAULT_BACKBONE) -> None:
        self.model = model

    def answer(self, question: str, hits: list) -> str:
        return openrouter_chat(self.model, [
            {"role": "system", "content": READER_SYSTEM_PROMPT},
            {"role": "user", "content": render_user_prompt(question, hits)},
        ]).strip()

    def answer_full_context(self, question: str, transcript: str) -> str:
        return openrouter_chat(self.model, [
            {"role": "system", "content": FC_SYSTEM_PROMPT},
            {"role": "user", "content": f"{transcript}\n\nQuestion: {question}"},
        ], max_tokens=256).strip()
