"""The README must document the operational traps the spec pins (§6, §10, §15).

These are cheap presence checks — they keep the doc honest, not exhaustive.
"""
from __future__ import annotations

from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_covers_the_load_bearing_facts() -> None:
    text = README.read_text()
    required = [
        "./lm_data",                         # the data-root mismatch trap (§10)
        "LM_DATA_ROOT",                      # data-root resolution
        "~/.lean_memory",                    # console default root
        "t_ref",                             # live-vs-replay temporal anchor
        "LM_API_KEY",                        # required in Docker mode
        "one namespace per project",         # concurrency guidance (§6/§15)
        "read-only",                         # the console's core claim (§1)
        "torch",                             # image-size note (§15)
    ]
    missing = [s for s in required if s not in text]
    assert not missing, f"README missing required topics: {missing}"


def test_readme_documents_both_modes() -> None:
    text = README.read_text().lower()
    assert "local" in text and "docker" in text
    assert "serve" in text and "mcp" in text
