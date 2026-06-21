"""Offline tests for the CLI demo agent (examples/chat.py).

All tests run with FakeEmbedder + a stubbed Anthropic client — no network, no
'anthropic' package required at import time.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

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
