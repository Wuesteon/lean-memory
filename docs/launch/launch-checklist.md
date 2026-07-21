# WP1 launch checklist — channel-by-channel runbook

Status as of 2026-07-21 (branch `wp1-launch`). Executes spec §3
(`docs/superpowers/specs/2026-07-08-strategic-direction-design.md`).
One narrative everywhere: **local-first, no server, time-travel history.**
Zero benchmark numbers in any copy; every channel links the repo README
quickstart; ~2 GB model download stated wherever an install is shown.

## Channel status

| # | Channel | Status | Blocking action | Who |
|---|---|---|---|---|
| 1 | MCP Registry | **LIVE** (0.2.0 active, `isLatest`; `server.json` install spec verified end-to-end) | v0.2.1 republish is automatic: `publish-mcp` workflow on tag push | CI |
| 2 | PyPI (core + console) | **LIVE** (0.2.0 both) — console page blank until 0.2.1 | Merge this branch → tag `v0.2.1` → `release` workflow publishes both via Trusted Publishing | maintainer + CI |
| 3 | `awesome-mcp-servers` PR | ready | Open PR (entry + mechanics in `launch-copy.md` §4) | agent or maintainer |
| 4 | Claude Code plugin marketplace | manifest valid + discovery metadata added | Submit form (steps below) **after merge** so the reviewed manifest is on `main` | maintainer (account-bound form) |
| 5 | Show HN | copy final (`launch-copy.md` §1) | Post from own account | maintainer |
| 6 | r/ClaudeAI + r/LocalLLaMA | copy final (`launch-copy.md` §2–3) | Post from own account | maintainer |

## Launch-day order

1. Merge `wp1-launch` → `main` (PR; review the amended copy — see “Decisions
   applied” below).
2. Tag and push `v0.2.1` → `release` + `publish-mcp` workflows fire (PyPI ×2 +
   registry). Verify: console PyPI page renders; registry shows 0.2.1.
3. Open the `awesome-mcp-servers` PR
   (fork → branch `add-lean-memory` → insert the §4 entry line in
   **Knowledge & Memory**, alphabetically after `wnbhr/being` → PR titled
   “Add lean-memory server 🤖🤖🤖” — the trailing robots opt into the list’s
   documented fast-track lane for agent-authored PRs).
4. Submit the plugin: `claude plugin validate .` (passes as of this branch),
   then the Console form at <https://platform.claude.com/plugins/submit>
   pointed at `github.com/Wuesteon/lean-memory`. Notes: Anthropic’s community
   marketplace has **no PR route** (`anthropics/claude-plugins-community`
   auto-closes PRs); catalog sync is nightly, so expect delay; the plugin
   `name` is immutable once published. Optional secondary venues: a PR to
   `ComposioHQ/awesome-claude-plugins`; `claudemarketplaces.com` auto-crawls
   public repos with a valid manifest — nothing to submit.
5. Show HN (§1: title + author first-comment).
6. r/ClaudeAI (§2), r/LocalLLaMA (§3).

## Acceptance verification (packet criterion: each listing’s install snippet ends in a working `memory_add`/`memory_search`)

Fresh-venv walkthrough against the **published** PyPI 0.2.0 artifacts,
2026-07-21 — all four advertised snippets PASS:

1. `pip install lean-memory` → `from lean_memory import Memory`; add/search
   returns the fact; supersession verified (new fact wins, old row keeps
   `is_latest=0` + `superseded_by`; `as_of` time-travel returns the old fact).
2. `pip install 'lean-memory[mcp]'` → real stdio MCP client against
   `lean-memory-mcp`: 7 tools listed (incl. all four maintenance tools),
   `memory_add` → `memory_search` round-trip asserted.
3. `pip install lean-memory-console` → entry point serves; tokenized URL;
   clean startup/shutdown.
4. Registry command `uvx --from 'lean-memory[mcp,models,extract]'
   lean-memory-mcp`: the `[mcp]`-variant run end-to-end over stdio;
   the full extras spec resolves cleanly (73 packages). Confirmed the bare
   `uvx --from lean-memory lean-memory-mcp` form crashes — the manifest
   correctly does not ship it.

## Decisions applied on this branch (flag on PR review if you disagree)

- **Superlative dropped** (all three posts): “no other memory product stages
  agent-proposed changes for approval” was adversarially refuted (shipping
  counterexamples gate agent memory saves behind approval). Replaced with a
  hedged, narrowed differentiator: offline maintenance over an embedded local
  store, every judgment call staged as an approvable diff.
- **r/LocalLLaMA de-numbered** (spec §1 “no number”): the measured “~15%
  escalation” figure and the “real measured numbers” framing are out; the
  calibration README link stays, reframed as methodology-only. Alternatives if
  you want them: cut the link entirely, or deliberately restore the numbers as
  an approved carve-out for that audience.
- **Positioning vs built-in Claude Code memory** added to r/ClaudeAI
  (cross-client, auditable in any SQLite browser, as-of queries).
- **“No Docker”** added to the OpenMemory-differentiation triads (spec risk
  list) in Show HN, r/ClaudeAI, the awesome one-liner, and the registry blurb.
- **README status note** updated to “first public release line is live”;
  shipped inside the v0.2.1 PyPI description.

## Post-launch

Start the **six-week demand read** (spec §4): strangers filing issues/PRs
(primary), ~200+ stars / rising PyPI downloads (directional). Signal → revisit
specialization + contributor scaffolding + deferred benchmarks; silence →
iterate positioning/channels, benchmarks as the retry lever.
