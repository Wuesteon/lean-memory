---
description: Stop the Docker lean-memory console (data volume is preserved).
---

Tear down the Docker console using the same packaged compose file. The named
`lm_data` volume is **not** removed, so your memories persist across restarts.

!`docker compose -f "$(lean-memory-console --print-compose-path)" down`

To also delete stored memories and the HF cache (irreversible), add `-v`
manually:
`docker compose -f "$(lean-memory-console --print-compose-path)" down -v`
