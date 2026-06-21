# CLI Demo Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a terminal chatbot (`examples/chat.py`) that uses lean-memory as its memory backend and Claude as the LLM, demonstrating the full add → retrieve → supersede memory loop across turns and restarts.

**Architecture:** A single-file CLI script holds a REPL loop. Each turn calls `mem.add(namespace, user_message)` to store facts, then `mem.search(namespace, user_message, k=3)` to retrieve relevant memories, formats them into a `## What I know about you` block, injects that block into the Claude system prompt, and prints the retrieved memories so the loop is visible. The `anthropic` SDK is imported lazily inside the LLM-call function so the module imports cleanly without the dependency (tests stub the client). All memory state persists via lean-memory's per-namespace SQLite files under a fixed `root` dir.

**Tech Stack:** Python 3.10+ (repo runs on 3.13), `lean_memory` (local package), `anthropic>=0.25` SDK (optional `examples` extra), `argparse` + `pytest` (stdlib + dev).

## Global Constraints

- `requires-python = ">=3.10"` — code must run on 3.10 through 3.13.
- Public memory API only: `from lean_memory import Memory`; `mem.add(namespace, text, t_ref=...)`; `mem.search(namespace, query, k=...)`.
- `RetrievedFact` is NESTED: a hit `h` exposes `h.fact.fact_text`, `h.fact.subject_id`, `h.fact.predicate`, `h.fact.object_literal`, `h.fact.valid_at`, and `h.final_score`. There is NO flat `h.fact_text` / `h.score`. Verified in `src/lean_memory/types.py` and `tests/test_spine.py`.
- Default offline backend: `Memory()` already defaults to `FakeEmbedder` + `IdentityReranker`. The `--no-real` flag forces these; without it, the demo tries to load `SentenceTransformerEmbedder` + `CrossEncoderReranker` and falls back to stubs if the `models` extra is missing.
- LLM model id: `claude-haiku-4-5-20251001`.
- `ANTHROPIC_API_KEY` missing → print a warning and echo the memory context instead of calling the API (no crash).
- Tests MUST run fully offline: stub the Anthropic client, use `FakeEmbedder`, never hit the network. The `anthropic` package is NOT installed in CI/venv, so `examples/chat.py` must import `anthropic` lazily (inside the call function), never at module top level.
- Namespace defaults to `"demo"`, overridable via `--namespace`.
- `examples/chat.py` target size ~120 lines.
- All file paths in commands are repo-relative to `/Users/wuesteon/research/lean-memory` (run pytest from the repo root; `pyproject.toml` sets `pythonpath=["src"]` and `testpaths=["tests"]`).

---

## File Structure

- `examples/chat.py` (create) — the demo CLI. Pure functions for formatting + LLM call, a `Memory`-construction helper, an `argparse` entrypoint, and a REPL loop. `anthropic` imported lazily inside `call_claude`.
- `tests/test_chat.py` (create) — offline tests: import the module, exercise `format_memory_block`, `build_system_prompt`, `make_memory`, the no-API-key fallback, and the full turn loop with a stubbed Anthropic client and `FakeEmbedder`.
- `pyproject.toml` (modify) — add `examples = ["anthropic>=0.25"]` to `[project.optional-dependencies]`.

The demo logic is split into small pure functions (`format_memory_block`, `build_system_prompt`, `call_claude`, `handle_turn`) so each is unit-testable without a terminal or network. The REPL `main()` wires them together and is exercised only indirectly.

---

### Task 1: Add the `examples` optional extra to pyproject.toml

**Files:**
- Modify: `pyproject.toml` (the `[project.optional-dependencies]` table, currently ending at the `dev` entry around lines 35-37)

**Interfaces:**
- Consumes: nothing.
- Produces: `pip install 'lean-memory[examples]'` resolves `anthropic>=0.25`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat.py` with just this test for now:

```python
"""Offline tests for the CLI demo agent (examples/chat.py).

All tests run with FakeEmbedder + a stubbed Anthropic client — no network, no
'anthropic' package required at import time.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_examples_extra_declares_anthropic():
    data = tomllib.loads(_PYPROJECT.read_text())
    extras = data["project"]["optional-dependencies"]
    assert "examples" in extras, "pyproject must declare an 'examples' extra"
    assert any(dep.startswith("anthropic") for dep in extras["examples"]), (
        f"examples extra must pin anthropic, got {extras['examples']}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_chat.py::test_examples_extra_declares_anthropic -v`
Expected: FAIL with `KeyError: 'examples'` (or AssertionError "pyproject must declare an 'examples' extra").

- [ ] **Step 3: Add the extra to pyproject.toml**

In `pyproject.toml`, inside `[project.optional-dependencies]`, add this block immediately after the `llm = [...]` entry and before `dev = [...]`:

```toml
# Terminal demo agent (examples/chat.py): Claude as the LLM via the anthropic SDK.
examples = [
    "anthropic>=0.25",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_chat.py::test_examples_extra_declares_anthropic -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/test_chat.py
git commit -m "feat(examples): declare anthropic extra for demo agent"
```

---

### Task 2: Memory formatting helpers

**Files:**
- Create: `examples/chat.py`
- Test: `tests/test_chat.py`

**Interfaces:**
- Consumes: `RetrievedFact` from `lean_memory` (nested: `h.fact.fact_text`, `h.final_score`).
- Produces:
  - `format_memory_block(hits: list) -> str` — returns a `## What I know about you` markdown block; one `- <fact_text>` bullet per hit. Empty list → the single line `"## What I know about you\n(nothing yet)"`.
  - `build_system_prompt(memory_block: str) -> str` — returns the full Claude system prompt embedding `memory_block`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat.py`:

```python
import importlib.util

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "format_memory_block or build_system_prompt" -v`
Expected: FAIL — `FileNotFoundError`/`ModuleNotFoundError` because `examples/chat.py` does not exist yet.

- [ ] **Step 3: Create examples/chat.py with the helpers**

Create `examples/chat.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "format_memory_block or build_system_prompt" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add examples/chat.py tests/test_chat.py
git commit -m "feat(examples): memory-block + system-prompt formatting helpers"
```

---

### Task 3: Claude call with graceful no-API-key fallback

**Files:**
- Modify: `examples/chat.py`
- Test: `tests/test_chat.py`

**Interfaces:**
- Consumes: `build_system_prompt` (Task 2).
- Produces:
  - `call_claude(client, system_prompt: str, user_message: str) -> str` — when `client is None`, returns a fallback string starting with `"[no ANTHROPIC_API_KEY]"` that includes the system prompt's memory block; otherwise calls `client.messages.create(model=MODEL, max_tokens=512, system=system_prompt, messages=[{"role": "user", "content": user_message}])` and returns the first text block.
  - `make_client()` -> a real `anthropic.Anthropic()` instance, or `None` if `ANTHROPIC_API_KEY` is unset or the `anthropic` package is not importable. `anthropic` is imported INSIDE this function (lazy).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat.py`:

```python
class _StubMessages:
    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.update(kwargs)
        block = type("Block", (), {"text": "stubbed reply"})()
        return type("Resp", (), {"content": [block]})()


class _StubAnthropic:
    """Minimal stand-in for anthropic.Anthropic — no network."""

    def __init__(self):
        self.calls = {}
        self.messages = _StubMessages(self.calls)


def test_call_claude_uses_client_and_returns_text():
    chat = _load_chat()
    client = _StubAnthropic()
    out = chat.call_claude(client, "SYS", "where do I work?")
    assert out == "stubbed reply"
    assert client.calls["model"] == chat.MODEL
    assert client.calls["system"] == "SYS"
    assert client.calls["messages"] == [
        {"role": "user", "content": "where do I work?"}
    ]


def test_call_claude_falls_back_without_client():
    chat = _load_chat()
    sys_prompt = chat.build_system_prompt(
        chat.format_memory_block([_StubHit("I work at Acme.")])
    )
    out = chat.call_claude(None, sys_prompt, "where do I work?")
    assert out.startswith("[no ANTHROPIC_API_KEY]")
    assert "I work at Acme." in out  # the memory context is echoed


def test_make_client_returns_none_without_key(monkeypatch):
    chat = _load_chat()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert chat.make_client() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "call_claude or make_client" -v`
Expected: FAIL with `AttributeError: module 'demo_chat' has no attribute 'call_claude'`.

- [ ] **Step 3: Add call_claude and make_client to examples/chat.py**

Append to `examples/chat.py` (after `build_system_prompt`):

```python
def make_client():
    """Build a real Anthropic client, or None if unusable (offline-safe).

    `anthropic` is imported lazily here so the module imports without the
    package installed (tests stub the client and never import this path).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print(
            "[warn] anthropic SDK not installed; run "
            "pip install 'lean-memory[examples]'. Falling back to echo mode.",
            file=sys.stderr,
        )
        return None
    return anthropic.Anthropic()


def call_claude(client, system_prompt: str, user_message: str) -> str:
    """Get an assistant reply. With no client, echo the memory context instead."""
    if client is None:
        return (
            "[no ANTHROPIC_API_KEY] I'd answer using the memory below, but no "
            f"API key is set, so here is the context I loaded:\n\n{system_prompt}"
        )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "call_claude or make_client" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add examples/chat.py tests/test_chat.py
git commit -m "feat(examples): Claude call with offline no-key fallback"
```

---

### Task 4: Memory construction with --no-real backend selection

**Files:**
- Modify: `examples/chat.py`
- Test: `tests/test_chat.py`

**Interfaces:**
- Consumes: `Memory` from `lean_memory`.
- Produces:
  - `make_memory(root: str, real: bool) -> Memory` — `real=False` builds `Memory(root=root)` (defaults: `FakeEmbedder` + `IdentityReranker`, zero downloads). `real=True` tries `SentenceTransformerEmbedder` + `CrossEncoderReranker`; on `ImportError` (missing `models` extra) it prints a warning and falls back to the offline defaults.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat.py`:

```python
from lean_memory.embed.fake import FakeEmbedder


def test_make_memory_no_real_uses_fake_embedder(tmp_path):
    chat = _load_chat()
    mem = chat.make_memory(root=str(tmp_path), real=False)
    try:
        assert isinstance(mem.embedder, FakeEmbedder)
    finally:
        mem.close()


def test_make_memory_no_real_roundtrips(tmp_path):
    """make_memory must return a working Memory: add then search round-trips."""
    chat = _load_chat()
    mem = chat.make_memory(root=str(tmp_path), real=False)
    try:
        mem.add("demo", "I work at Acme.", t_ref=1_700_000_000_000)
        hits = mem.search("demo", "where does the user work?", k=3)
        assert any("Acme" in h.fact.fact_text for h in hits)
    finally:
        mem.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "make_memory" -v`
Expected: FAIL with `AttributeError: module 'demo_chat' has no attribute 'make_memory'`.

- [ ] **Step 3: Add make_memory to examples/chat.py**

Append to `examples/chat.py` (after `call_claude`):

```python
def make_memory(root: str, real: bool) -> Memory:
    """Construct the Memory engine. real=False forces the offline stubs."""
    if not real:
        return Memory(root=root)  # FakeEmbedder + IdentityReranker by default
    try:
        from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder
        from lean_memory.retrieve.rerank import CrossEncoderReranker
    except ImportError:
        print(
            "[warn] real backends need the 'models' extra "
            "(pip install 'lean-memory[models]'). Using offline stubs.",
            file=sys.stderr,
        )
        return Memory(root=root)
    return Memory(
        root=root,
        embedder=SentenceTransformerEmbedder(),
        reranker=CrossEncoderReranker(),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "make_memory" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add examples/chat.py tests/test_chat.py
git commit -m "feat(examples): backend selection with --no-real fallback"
```

---

### Task 5: Single-turn handler (add → search → print → reply)

**Files:**
- Modify: `examples/chat.py`
- Test: `tests/test_chat.py`

**Interfaces:**
- Consumes: `format_memory_block`, `build_system_prompt`, `call_claude` (Tasks 2-3), a `Memory` instance.
- Produces:
  - `handle_turn(mem, client, namespace: str, user_message: str) -> tuple[str, list]` — calls `mem.add(namespace, user_message)`, then `hits = mem.search(namespace, user_message, k=3)`, prints the retrieved memories to stdout (`Memory loaded (N):` then one `  • <fact_text>  [score=...]` line per hit), builds the system prompt, calls `call_claude`, and returns `(reply, hits)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat.py`:

```python
def test_handle_turn_stores_then_retrieves_and_prints(tmp_path, capsys):
    chat = _load_chat()
    mem = chat.make_memory(root=str(tmp_path), real=False)
    client = _StubAnthropic()
    try:
        # Turn 1: teach a fact.
        reply1, hits1 = chat.handle_turn(mem, client, "demo", "I work at Acme.")
        assert reply1 == "stubbed reply"
        # Turn 2: ask about it — the fact must be retrieved and printed.
        reply2, hits2 = chat.handle_turn(
            mem, client, "demo", "where do I work?"
        )
        assert any("Acme" in h.fact.fact_text for h in hits2)
        out = capsys.readouterr().out
        assert "Memory loaded" in out
        assert "Acme" in out
        # The system prompt sent to Claude carried the memory block.
        assert "Acme" in client.calls["system"]
    finally:
        mem.close()


def test_handle_turn_supersession_changes_answer(tmp_path):
    """Update the fact in a later turn → search returns only the new employer."""
    chat = _load_chat()
    mem = chat.make_memory(root=str(tmp_path), real=False)
    client = _StubAnthropic()
    try:
        chat.handle_turn(mem, client, "demo", "I work at Acme.")
        chat.handle_turn(mem, client, "demo", "I work at Globex now.")
        _, hits = chat.handle_turn(mem, client, "demo", "where do I work?")
        texts = " ".join(h.fact.fact_text for h in hits)
        assert "Globex" in texts
        assert "Acme" not in texts
    finally:
        mem.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "handle_turn" -v`
Expected: FAIL with `AttributeError: module 'demo_chat' has no attribute 'handle_turn'`.

- [ ] **Step 3: Add handle_turn to examples/chat.py**

Append to `examples/chat.py` (after `make_memory`):

```python
def handle_turn(mem, client, namespace: str, user_message: str):
    """One full turn: store, retrieve, show what loaded, answer."""
    mem.add(namespace, user_message)  # t_ref defaults to now_ms() inside Memory
    hits = mem.search(namespace, user_message, k=3)

    # Make the engine visible: print exactly what memory was loaded.
    print(f"  [Memory loaded ({len(hits)}):]")
    for h in hits:
        print(f"    • {h.fact.fact_text}  [score={h.final_score:.3f}]")

    memory_block = format_memory_block(hits)
    system_prompt = build_system_prompt(memory_block)
    reply = call_claude(client, system_prompt, user_message)
    return reply, hits
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "handle_turn" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add examples/chat.py tests/test_chat.py
git commit -m "feat(examples): single-turn handler with visible memory loading"
```

---

### Task 6: CLI entrypoint and REPL loop

**Files:**
- Modify: `examples/chat.py`
- Test: `tests/test_chat.py`

**Interfaces:**
- Consumes: `make_memory`, `make_client`, `handle_turn` (Tasks 3-5).
- Produces:
  - `parse_args(argv: list[str]) -> argparse.Namespace` — flags `--namespace` (default `"demo"`), `--root` (default `"./examples_data"`), `--no-real` (store_true; `args.real == not args.no_real` via `dest="real"`/`action="store_false"`).
  - `main(argv=None) -> int` — parses args, builds memory + client, runs a `input()` REPL calling `handle_turn` per line until EOF/`exit`/`quit`, returns `0`. Guarded by `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat.py`:

```python
def test_parse_args_defaults():
    chat = _load_chat()
    args = chat.parse_args([])
    assert args.namespace == "demo"
    assert args.real is True  # real backends by default


def test_parse_args_no_real_and_namespace():
    chat = _load_chat()
    args = chat.parse_args(["--no-real", "--namespace", "bob"])
    assert args.real is False
    assert args.namespace == "bob"


def test_main_runs_one_turn_then_eof(tmp_path, monkeypatch, capsys):
    chat = _load_chat()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # echo mode, offline

    lines = iter(["I work at Acme.", EOFError])

    def fake_input(prompt=""):
        nxt = next(lines)
        if nxt is EOFError:
            raise EOFError
        return nxt

    monkeypatch.setattr("builtins.input", fake_input)
    code = chat.main(["--no-real", "--root", str(tmp_path), "--namespace", "demo"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Memory loaded" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "parse_args or main_runs" -v`
Expected: FAIL with `AttributeError: module 'demo_chat' has no attribute 'parse_args'`.

- [ ] **Step 3: Add parse_args and main to examples/chat.py**

Append to `examples/chat.py` (after `handle_turn`):

```python
def parse_args(argv):
    p = argparse.ArgumentParser(description="lean-memory terminal demo agent")
    p.add_argument(
        "--namespace", default=DEFAULT_NAMESPACE,
        help="memory tenant id (persists across restarts); default 'demo'",
    )
    p.add_argument(
        "--root", default=DEFAULT_ROOT,
        help="directory for the per-namespace SQLite files",
    )
    p.add_argument(
        "--no-real", dest="real", action="store_false",
        help="use offline FakeEmbedder/IdentityReranker (zero downloads)",
    )
    p.set_defaults(real=True)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    mem = make_memory(root=args.root, real=args.real)
    client = make_client()
    if client is None:
        print(
            "[warn] running without Claude (no ANTHROPIC_API_KEY); replies echo "
            "the memory context.",
            file=sys.stderr,
        )
    print(
        f"lean-memory demo — namespace={args.namespace!r}, root={args.root!r}. "
        "Type 'exit' or Ctrl-D to quit."
    )
    try:
        while True:
            try:
                user_message = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_message:
                continue
            if user_message.lower() in {"exit", "quit"}:
                break
            reply, _ = handle_turn(mem, client, args.namespace, user_message)
            print(f"bot> {reply}\n")
    finally:
        mem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_chat.py -k "parse_args or main_runs" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the FULL test file and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_chat.py -v`
Expected: PASS (all `test_chat.py` tests, 13 passed).

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — the pre-existing 32 tests plus the new ones, no failures, no network access.

- [ ] **Step 6: Commit**

```bash
git add examples/chat.py tests/test_chat.py
git commit -m "feat(examples): CLI entrypoint and REPL loop for demo agent"
```

---

### Task 7: Persistence-across-restart proof

**Files:**
- Test: `tests/test_chat.py` (no source change — this verifies an existing guarantee)

**Interfaces:**
- Consumes: `make_memory` (Task 4), the public `Memory` add/search API.
- Produces: nothing new; a regression test pinning that a fact stored under a namespace + root survives constructing a brand-new `Memory` over the same root.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat.py`:

```python
def test_memory_persists_across_restart(tmp_path):
    """Same root + namespace → a fresh Memory still finds the earlier fact."""
    chat = _load_chat()
    client = _StubAnthropic()

    mem1 = chat.make_memory(root=str(tmp_path), real=False)
    chat.handle_turn(mem1, client, "demo", "I work at Acme.")
    mem1.close()  # simulate process exit

    mem2 = chat.make_memory(root=str(tmp_path), real=False)
    try:
        _, hits = chat.handle_turn(mem2, client, "demo", "where do I work?")
        assert any("Acme" in h.fact.fact_text for h in hits)
    finally:
        mem2.close()
```

- [ ] **Step 2: Run test to verify it fails or passes meaningfully**

Run: `.venv/bin/python -m pytest tests/test_chat.py::test_memory_persists_across_restart -v`
Expected: PASS immediately (persistence is provided by lean-memory's SQLite files; this test guards against a future regression in `make_memory`/`handle_turn` that would break it). If it FAILS, the bug is in `make_memory` reusing an in-memory-only path — fix `make_memory` to pass `root` through unchanged.

- [ ] **Step 3: Commit**

```bash
git add tests/test_chat.py
git commit -m "test(examples): pin memory persistence across restarts"
```

---

## Self-Review

**1. Spec coverage** — every requirement maps to a task:

- `mem.add` per user turn → Task 5 (`handle_turn`), tested in `test_handle_turn_stores_then_retrieves_and_prints`.
- `mem.search(..., k=3)` before assistant turn → Task 5, same test asserts retrieval.
- Inject memories into the Claude system prompt as `## What I know about you` → Task 2 (`format_memory_block` + `build_system_prompt`), Task 5 asserts `"Acme" in client.calls["system"]`.
- Print retrieved memories visibly → Task 5 prints `Memory loaded (N)` + bullets; `capsys` asserts it.
- Persist across restarts (same root + namespace) → Task 7.
- Namespace defaults to `"demo"`, `--namespace` override → Task 6 (`parse_args` tests).
- `--no-real` uses offline stubs → Task 4 + Task 6 (`test_make_memory_no_real_uses_fake_embedder`, `test_parse_args_no_real_and_namespace`).
- anthropic SDK, model `claude-haiku-4-5-20251001` → Task 3 (`MODEL` constant, `call_claude` asserts `client.calls["model"] == chat.MODEL`).
- Graceful fallback with no `ANTHROPIC_API_KEY` → Task 3 (`make_client` returns `None`, `call_claude` echoes context); `test_call_claude_falls_back_without_client`, `test_make_client_returns_none_without_key`.
- `examples/chat.py` ~120 lines → the appended blocks total ~115 lines.
- `tests/test_chat.py` with stubbed Anthropic client + offline Memory → `_StubAnthropic`, `FakeEmbedder` via `make_memory(real=False)`.
- pyproject `examples = ["anthropic>=0.25"]` extra → Task 1.

**2. Placeholder scan** — no "TBD"/"add error handling"/"similar to Task N". Every code step shows complete code. The two warnings printed by `make_client`/`make_memory` are concrete strings, not placeholders.

**3. Type consistency** — `RetrievedFact` accessed as `h.fact.fact_text` and `h.final_score` everywhere (matches `src/lean_memory/types.py` and `tests/test_spine.py`; the flat shape in the prompt brief is incorrect and is NOT used). `handle_turn` returns `(reply, hits)` and all callers unpack the tuple. `make_memory(root, real)`, `make_client()`, `call_claude(client, system_prompt, user_message)`, `format_memory_block(hits)`, `build_system_prompt(memory_block)`, `parse_args(argv)`, `main(argv)` signatures are identical across their defining task and all call sites. `MODEL`, `DEFAULT_NAMESPACE`, `DEFAULT_ROOT` constants defined once in Task 2's module header and reused.

**4. Offline safety** — `anthropic` is imported only inside `make_client` (lazy), so `_load_chat()` imports `examples/chat.py` without the package installed. Tests construct `_StubAnthropic` directly or pass `client=None`; no test triggers `make_client`'s import path with a key set. `make_memory(real=False)` keeps everything on `FakeEmbedder`. No test reaches the network.
