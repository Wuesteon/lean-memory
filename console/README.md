# lean-memory-console

Agent-first, read-only verification console for
[lean-memory](../README.md). Agents write and search memory; the human opens
the console to verify what was stored.

## Develop

    python3 -m venv console/.venv
    console/.venv/bin/pip install -e . -e './console[dev]'
    console/.venv/bin/python -m pytest console/tests -v

Runs fully offline on stub backends (`LM_CONSOLE_MODELS=stub`).
