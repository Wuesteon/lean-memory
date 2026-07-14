---
description: Show the resolved data root, its namespaces, and connect snippets — with the ./lm_data mismatch warning.
---

Report where memory actually lives and how to connect, so the human never
inspects an empty root while the agent wrote elsewhere.

1. Print the resolved data root (the console applies `--root` >
   `LM_DATA_ROOT` > `~/.lean_memory`):

   !`echo "Resolved root: ${LM_DATA_ROOT:-$HOME/.lean_memory}"`

   Then enumerate namespace `.db` files under that root (skipping `_*.db`):

   !`ls -1 "${LM_DATA_ROOT:-$HOME/.lean_memory}"/*.db 2>/dev/null | grep -v '/_' || echo "(no namespaces yet)"`

2. **./lm_data mismatch warning.** The core engine's *own* default root is
   `./lm_data`, not `~/.lean_memory`. If `./lm_data` exists in the current
   project but is not the served root, the human would silently inspect an
   empty root. Warn when it exists:

   !`test -d ./lm_data && echo "WARNING: ./lm_data exists here. Your agent may have written memories to ./lm_data (the engine's default root), not ${LM_DATA_ROOT:-$HOME/.lean_memory}. Run '/memory:ui' with --root ./lm_data to inspect it." || echo "No ./lm_data in this directory."`

3. **Connect snippets.**
   - Local observing MCP (this plugin already wires it via `.mcp.json`):
     `uvx lean-memory-console mcp`
   - Open the console: `/memory:ui`
   - Docker HTTP (after `/memory:server-up`):
     ```
     claude mcp add --transport http lean-memory http://127.0.0.1:8377/mcp \
       --header "Authorization: Bearer $LM_API_KEY"
     ```

Guidance: use **one namespace per project/session** — cross-process writers on
a single namespace serialize via retry, not a lock manager.
