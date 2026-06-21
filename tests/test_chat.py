"""Offline tests for the CLI demo agent (examples/chat.py).

All tests run with FakeEmbedder + a stubbed Anthropic client — no network, no
'anthropic' package required at import time.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
_CHAT_PATH = Path(__file__).resolve().parent.parent / "examples" / "chat.py"


def _load_chat():
    """Import examples/chat.py by path (examples/ is not an installed package)."""
    spec = importlib.util.spec_from_file_location("demo_chat", _CHAT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubHit:
    """Mimics lean_memory.RetrievedFact's nested shape (h.fact.fact_text)."""

    def __init__(self, fact_text: str, score: float = 1.0):
        self.fact = type("F", (), {"fact_text": fact_text})()
        self.final_score = score


def test_format_memory_block_lists_facts():
    chat = _load_chat()
    hits = [_StubHit("I work at Acme."), _StubHit("I have a dog named Rex.")]
    block = chat.format_memory_block(hits)
    assert block.startswith("## What I know about you")
    assert "- I work at Acme." in block
    assert "- I have a dog named Rex." in block


def test_format_memory_block_empty():
    chat = _load_chat()
    block = chat.format_memory_block([])
    assert block.startswith("## What I know about you")
    assert "(nothing yet)" in block


def test_build_system_prompt_embeds_memory_block():
    chat = _load_chat()
    block = chat.format_memory_block([_StubHit("I live in Berlin.")])
    prompt = chat.build_system_prompt(block)
    assert "I live in Berlin." in prompt
    assert "## What I know about you" in prompt


def test_examples_extra_declares_anthropic():
    data = tomllib.loads(_PYPROJECT.read_text())
    extras = data["project"]["optional-dependencies"]
    assert "examples" in extras, "pyproject must declare an 'examples' extra"
    assert any(dep.startswith("anthropic") for dep in extras["examples"]), (
        f"examples extra must pin anthropic, got {extras['examples']}"
    )
