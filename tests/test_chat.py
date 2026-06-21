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
