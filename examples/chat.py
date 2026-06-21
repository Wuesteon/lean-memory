"""Terminal chatbot demo for lean-memory.

Shows the full memory loop end to end:
  - tell it a fact in one turn        → mem.add() extracts + stores it
  - ask about it in a later turn      → mem.search() retrieves it
  - update the fact                   → supersession swaps the answer
  - restart with the same --namespace → memory persists (same SQLite files)

The LLM is Claude (anthropic SDK, model claude-haiku-4-5-20251001). With no
ANTHROPIC_API_KEY set the demo still runs: it prints the retrieved memory
context instead of calling the API, so you can watch the engine work offline.

Run:
    pip install -e '.[examples,models]'
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/chat.py                 # real embedder + reranker if installed
    python examples/chat.py --no-real       # offline stubs, zero downloads
    python examples/chat.py --namespace bob # separate memory tenant
"""

from __future__ import annotations

import argparse
import os
import sys

from lean_memory import Memory

MODEL = "claude-haiku-4-5-20251001"
DEFAULT_NAMESPACE = "demo"
DEFAULT_ROOT = "./examples_data"


def format_memory_block(hits: list) -> str:
    """Render retrieved memories as a markdown block for the system prompt."""
    header = "## What I know about you"
    if not hits:
        return f"{header}\n(nothing yet)"
    lines = [f"- {h.fact.fact_text}" for h in hits]
    return header + "\n" + "\n".join(lines)


def build_system_prompt(memory_block: str) -> str:
    """Wrap the memory block in a system prompt that tells Claude to use it."""
    return (
        "You are a helpful assistant with a long-term memory of the user.\n"
        "Use the facts below when they are relevant. If a fact answers the "
        "question, rely on it. Do not invent facts that are not listed.\n\n"
        f"{memory_block}"
    )
