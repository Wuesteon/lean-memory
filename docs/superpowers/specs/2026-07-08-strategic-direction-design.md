# Strategic Direction — Quality Gate, Then Launch

Date: 2026-07-08. Status: approved design.
Companion docs: `docs/phase2-learnings.md` (why the engine-fix backlog exists),
`docs/superpowers/phase2-HANDOFF.md` (operational runbook for the fixes),
`ARCHITECTURE.md` (implementation status).

## Decision summary

lean-memory's goal is **real OSS adoption**: strangers installing it, filing
issues, and shipping agents with it as their memory layer. The first wedge is
**coding-agent users via MCP** (Claude Code, Claude Desktop, other MCP
clients). The engine stays a **generic memory library** — MCP is a
distribution channel, not a product pivot ("channel only"; specialization is
re-decided after launch, with data). The chosen strategy is **Pure A: close a
small, hard quality gate on the first-run experience, then launch across MCP
channels.** Contributor scaffolding and benchmark runs are deferred.

Decisions made along the way, with the reasoning:

| Question | Decision | Why |
|---|---|---|
| What is success? | Real OSS adoption | Not benchmark credibility, not a startup seed — usage by strangers |
| First wedge? | MCP coding-agent users | MCP server already exists; distribution is listable channels, not sales |
| Phase 2 backlog? | Engine fixes yes, benchmark runs later | The three flaws hurt real usage too; scores are a credibility follow-up, not the critical path |
| Capacity? | Solo, open to bringing in help | Contributability matters eventually, but contributors follow users — deferred past launch |
| Specialize for coding agents? | No — channel only | Smallest bet; test demand before bending the product |
| Sequencing? | Quality gate → launch (Pure A) | The current default install returns semantically meaningless results (FakeEmbedder); driving traffic before fixing that burns the one first impression |

## 1. Positioning — the reason for existence

**lean-memory is the SQLite of agent memory: memory that's just a file.**
Embedded library, one SQLite file per namespace, no server, no daemon, no
cloud key, ADD-only history queryable at any past point in time
(`as_of`).

- mem0 is the universal hosted memory layer (60K stars, 21 frameworks,
  managed cloud). lean-memory does not chase that. It is the thing you
  `pip install` that never phones home.
- The wedge pitch, concretely: *your coding agent's memory lives on your
  machine, next to your code — portable across agents, inspectable with any
  SQLite browser, and it can answer "what did I tell you, and when."*
- At launch we do not compete on benchmark scores. We compete on trust,
  locality, and zero-ops. (Published memory-benchmark numbers swing 25–54
  points under independent re-evaluation — see phase2-learnings assumption
  #11 — so "no number" beats "a bad number", and our eventual number will be
  frozen-judge reproducible.)

## 2. Quality gate — definition of "launch-ready"

Six items. Launch waits for all six; nothing else joins the list.

1. **Real-by-default for the MCP path.** The canonical MCP install becomes
   `pip install 'lean-memory[mcp,models]'` (Qwen3-Embedding-0.6B +
   Ettin-32M). README states the model download size honestly. Bare install
   and the test suite keep the offline stubs.
   *Done when:* fresh venv → follow the README MCP section → add 3 facts →
   search returns the semantically right one.
2. **Escalation recalibration** (phase2-learnings next-step #1, unchanged):
   offline StubTyper probe of escalation vs. (`typing_threshold`,
   `conf_threshold`) on real LongMemEval turns → operating point <20% →
   validate with `bench/bet2_ablation.py --sweep --real` and its three gates
   → re-freeze constants in `bench/bet2_goldset.py`.
   *Done when:* gates pass and measured escalation on the conversational
   probe is <20%. (UX framing: this is the difference between `memory_add`
   feeling instant and stalling ~8s for `[llm]` users.)
3. **Extraction granularity** (next-step #2): calibrate the GLiNER threshold
   alongside item 2 so `fact_text` stops being whole utterances
   (~8 facts/turn today).
   *Done when:* facts read as facts, not paragraphs, on a sample of real
   conversational turns.
4. **Recency anchoring** (next-step #3): `Memory.search` forwards `now`, or
   decay anchors to corpus time.
   *Done when:* a regression test shows a last-month fact outranking an
   equally relevant last-year fact on historical data. ("What did I tell you
   last week" is the coding-agent query shape; the 0.2 recency term must not
   be dead.)
5. **Security housekeeping before any public attention:** rotate the
   OpenRouter key and HF token (`bench/.phase2_cache/*.key|*.token` — both
   passed through a chat session); merge `phase2-eval-harness` after the
   final whole-branch review (superpowers flow; deferred-minors ledger in
   `.superpowers/sdd/progress.md`).
6. **Two-minute quickstart:** README MCP section rewritten as copy-paste
   config for Claude Code and Claude Desktop, plus one demo GIF of the
   add → restart → recall loop.

## 3. Launch plan

Listings and assets are prepared during gate work; launch is a button-press
once the gate closes. Channels in order of expected yield:

1. Official MCP Registry listing
2. `awesome-mcp-servers` PR
3. Claude Code plugin marketplace
4. PyPI metadata polish (keywords, badges, project URLs)
5. Show HN — "lean-memory: agent memory that's just a SQLite file"
6. r/ClaudeAI and r/LocalLLaMA posts

One narrative everywhere: local-first, no server, time-travel history. Tag a
release and start a CHANGELOG.

## 4. Success criteria and the decision point

Read the demand signal **six weeks after launch**:

- **Primary:** strangers filing issues or PRs (real usage).
- **Directional:** ~200+ GitHub stars; rising PyPI download trend.

Decision rule:

- **Signal →** revisit "channel only" with data: coding-agent specialization
  features, contributor scaffolding (CONTRIBUTING.md, tagged issues, public
  roadmap), and then the deferred Phase 2 benchmark runs as the credibility
  layer.
- **Silence →** iterate on positioning and channels first, not the engine;
  the frozen-judge benchmark numbers become the retry lever.

No new engine work between launch and the six-week read.

## 5. Out of scope until the six-week read

LongMemEval/LoCoMo runs, coding-agent specialization (auto-capture hooks,
session summarizers), framework integrations (LangChain/CrewAI/etc.),
contributor program, hosted anything, int8 vectors, LanceStore.

## Risks (named, not solved)

- **mem0's OpenMemory competes in the same MCP channel.** Differentiation
  must stay crisp: no Docker, no server, single file, temporal queries.
- **Claude Code's built-in memory covers part of the itch.** Position as
  cross-agent, portable, and auditable rather than tied to one client.
- **Model download friction.** The `[models]` extra pulls ~1GB+ of weights on
  first run; the README must set that expectation before install, and the
  quickstart must still complete in ~2 minutes on a warm cache.

## Next step

Invoke the `superpowers:writing-plans` skill to turn the quality gate
(section 2) into an ordered implementation plan. Items 2–4 are the existing
phase2-learnings backlog and follow the handoff runbook; items 1, 5, 6 are
new packaging/housekeeping work.
