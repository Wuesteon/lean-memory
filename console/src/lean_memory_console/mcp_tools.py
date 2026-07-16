"""Shared MCP tool registration for the console's two surfaces (design spec §6.3).

The console ships the SAME memory + maintenance tools on both its stdio server
(``observe_mcp.py`` — what the plugin runs) and its Docker HTTP mount
(``routes/mcp.py``). Registering them once here, against a passed-in FastMCP + the
gateway, keeps tool names, signatures, and return shapes IDENTICAL across the two —
and identical to the core stdio server (``lean_memory.mcp_server``), which is the §6.3
requirement and the v0.1.3 manifest-parity lesson.

Every tool reaches the engine ONLY through ``EngineGateway`` (spec §1.3.8): the four
maintenance methods (``maintain``/``review_queue``/``decide``/``promote``) each wrap
``retry_busy`` + the per-namespace asyncio lock + the single worker thread, exactly
like ``add``/``search``.

The review-workflow PROMPT lives on the stdio server only (``register_review_prompt``)
— MCP prompt surfacing is a stdio-client capability; the plugin command file is the
portable path for everyone else (§6.4).
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .engine import EngineGateway

# The prompt text: the client agent runs THIS workflow. It MUST NOT decide any
# proposal without an explicit user verdict, and batch verbs ("approve all exact
# dedups") map only to explicit user statements (§6.4). Kept as a module constant so
# the stdio prompt and any future surface share one canonical wording.
REVIEW_PROMPT_TEXT = """\
You are helping the user review staged sleep-time memory-maintenance proposals for a \
namespace. Maintenance has already run and STAGED judgment calls (near-duplicate \
merges, summaries, evictions) as *proposals*; nothing has changed in memory yet. Your \
job is to walk the user through them and record ONLY the decisions the user explicitly \
makes.

HARD RULE — you may NOT decide any proposal on your own. Every approve / reject / edit \
/ promote MUST come from an explicit user verdict in this conversation. If the user has \
not stated a verdict for an item, leave it pending. A batch verb (e.g. "approve all \
exact dedups", "reject every eviction") is valid ONLY when the user says it in those \
words — never infer a batch decision from a single example or from silence. Silence is \
not consent; unreviewed proposals expire on their own.

Workflow:
1. Call `memory_review_queue(namespace=..., limit=...)` to fetch the pending queue. It \
   comes grouped by subject entity, each proposal carrying its evidence payload.
2. Present the proposals to the user, batched by entity and kind, showing the evidence \
   (the before/after texts, cosine for near-dups, source facts for summaries, the \
   value signals for evictions). Keep it scannable.
3. Collect the user's EXPLICIT verdicts. Ask when a verdict is missing or ambiguous. Do \
   not proceed on an item the user has not ruled on.
4. For each item the user decided, call \
   `memory_review_decide(namespace=..., proposal_id=..., decision=..., edited_text=...)` \
   with decision in {approve, reject, edit, promote}. `edited_text` applies only to a \
   summarize proposal the user chose to reword before approving.
5. Summarize what was applied, what was rejected, and what remains pending (untouched, \
   still awaiting the user).
"""


def register_maintenance_tools(mcp: FastMCP, gateway: EngineGateway) -> None:
    """Register the four §6.3 maintenance tools on `mcp`, routed through `gateway`.

    Identical names/signatures to the core stdio server: memory_maintenance_run
    (dry-run default), memory_maintenance_status, memory_review_queue,
    memory_review_decide.
    """

    @mcp.tool()
    async def memory_maintenance_run(
        namespace: str, apply: bool = False
    ) -> dict[str, Any]:
        """Run one sleep-time maintenance pass (§6.3). DRY-RUN by default (apply=False):
        computes the would-do report with zero writes. apply=True runs the auto band
        and stages proposals. Symmetric with the CLI. NOTE: the LM_MAINT_AUTO
        auto-spawn path runs `--apply --auto-only` (auto band only, no proposals) —
        only interactive apply=True grows the review queue. Returns the run summary."""
        return await gateway.maintain(namespace, apply=apply)

    @mcp.tool()
    async def memory_maintenance_status(namespace: str) -> dict[str, Any]:
        """Report the namespace's maintenance ledger — runs + pending proposals (§6.3).

        A pure model-free ledger read, shaped identically to the core server's status
        tool ({namespace, runs, pending_proposals, last_run}). Use it to see whether
        maintenance is due and how many proposals wait."""
        return await gateway.maintenance_status(namespace)

    @mcp.tool()
    async def memory_review_queue(
        namespace: str, kind: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """List pending maintenance proposals, grouped by entity, with evidence (§6.3).

        `kind` filters to 'dedup_near' | 'summarize' | 'evict'. Each proposal includes
        its parsed evidence payload so the reviewer sees exactly what would change."""
        groups = await gateway.review_queue(namespace, kind=kind, limit=limit)
        # Round-trip through JSON so numpy/None edge values serialize cleanly, matching
        # the core server's json.dumps(..., default=str) shape.
        return json.loads(json.dumps({"groups": groups}, default=str))

    @mcp.tool()
    async def memory_review_decide(
        namespace: str,
        proposal_id: str,
        decision: str,
        edited_text: str | None = None,
    ) -> dict[str, Any]:
        """Decide a maintenance proposal: approve | reject | edit | promote (§6.3).

        approve applies at decide-time with apply-time re-validation; reject leaves the
        spine byte-identical; edit (summarize only) approves the human-edited text;
        promote (evict only) rejects the eviction and lifts the fact to the hot tier."""
        result = await gateway.decide(
            namespace, proposal_id, decision, edited_text=edited_text
        )
        return json.loads(json.dumps(result, default=str))


def register_review_prompt(mcp: FastMCP) -> None:
    """Register the `review-memory-maintenance` prompt on a stdio FastMCP (§6.4).

    The prompt hands the client agent the review workflow but FORBIDS it from deciding
    without an explicit user verdict. Stdio-only: prompt surfacing is a stdio-client
    capability; the plugin command file is the portable path (§6.4)."""

    @mcp.prompt(name="review-memory-maintenance")
    def review_memory_maintenance(namespace: str = "") -> str:
        """Walk the user through staged memory-maintenance proposals and record only the
        decisions the user explicitly makes."""
        header = (
            f"Namespace to review: {namespace}\n\n" if namespace else ""
        )
        return header + REVIEW_PROMPT_TEXT
