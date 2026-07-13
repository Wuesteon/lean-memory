---
description: Launch the local lean-memory console and open it in the browser.
---

Start the transient read-only console over the resolved data root and open the
tokened URL. The server binds `127.0.0.1`, prints a URL containing a
single-use session token, and runs until you press Ctrl-C.

Run the console in the background and surface its URL:

!`lean-memory-console serve`

Notes for the user:
- The console is **read-only** over stored memory content. The only write it
  performs is the manual test-search box, which runs a real engine search and
  therefore bumps access stats (this is observability of live search, not a
  memory mutation).
- It serves exactly one data root: `--root` > `LM_DATA_ROOT` > `~/.lean_memory`.
  If your agent wrote to the engine's own default `./lm_data`, pass
  `--root ./lm_data` so you inspect the right root (see `/memory:status`).
- The session token dies with the process. Closing the terminal (Ctrl-C) stops
  the server.

If a browser did not open automatically, open the printed
`http://127.0.0.1:8377/?token=…` URL manually.
