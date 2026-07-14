---
description: Start the single-tenant lean-memory console in Docker (full image, real models).
---

Bring up the Docker console using the compose file packaged inside the
installed `lean-memory-console` wheel (the single source of truth — the plugin
ships no compose copy).

`LM_API_KEY` is **required** in Docker mode; the container refuses to boot
without it. Set one if you have not already (e.g. `export LM_API_KEY=$(openssl rand -hex 24)`).

!`docker compose -f "$(lean-memory-console --print-compose-path)" up -d`

After it starts, connect an agent over streamable-HTTP MCP:

```
claude mcp add --transport http lean-memory http://127.0.0.1:8377/mcp \
  --header "Authorization: Bearer $LM_API_KEY"
```

Open the console UI at `http://127.0.0.1:8377/` and authenticate with the same
`LM_API_KEY`. Stop it with `/memory:server-down`.
