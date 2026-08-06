"""Shared MCP tool registration for the console's two surfaces (design spec §6.3).

The console ships the SAME memory + maintenance tools on both its stdio server
(``observe_mcp.py`` — what the plugin runs) and its Docker HTTP mount
(``routes/mcp.py``). Registering them once here, against a passed-in server + the
gateway, keeps tool names, signatures, return shapes AND tool metadata IDENTICAL
across the two — and identical to the core stdio server (``lean_memory.mcp_server``),
which is the §6.3 requirement and the v0.1.3 manifest-parity lesson.

Metadata (WP14, the console half of core's WP13): every tool declares honest
``ToolAnnotations``, every schema parameter carries a description, and every
description says WHEN to reach for the tool and names its siblings — that is all a
directory (Glama et al.) or an agent ever sees of a tool. This is pinned by
``console/tests/test_mcp_tool_metadata.py``, which also asserts the two surfaces send
byte-identical metadata; keep it green when editing anything below.

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
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ._mcp_compat import MCPServerType

from .engine import EngineGateway

# Every console tool takes `namespace` with identical semantics, so the schema
# description is shared. Each namespace is one SQLite file under the console's
# data root (BET 4).
_NS = Annotated[
    str,
    Field(
        description=(
            "Isolation key for one memory store. Each namespace is a separate "
            "local SQLite file under the console's data root; namespaces never "
            "see each other's facts. Use one per agent, project, or user whose "
            "memory must stay separate. Created on first access."
        )
    ),
]

# Annotation baseline for both surfaces: every tool works on local SQLite files
# under the console's data root — a closed world, so openWorldHint=False
# everywhere. Nothing here is destructive: the spine is ADD-only (supersession
# retains history) and the console deliberately ships no memory_clear (§6).

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


def register_memory_tools(mcp: MCPServerType, gateway: EngineGateway) -> None:
    """Register the two observing memory tools on `mcp`, routed through `gateway`.

    Shared by BOTH console surfaces so neither their behavior nor their metadata can
    drift: the stdio wrapper (``observe_mcp.build_mcp``) and the Docker HTTP mount
    (``routes.mcp._build_http_mcp``) register exactly these definitions.

    A deliberate superset of the core stdio server: memory_add gains source/t_ref and a
    structured return; memory_clear is intentionally absent (no deletion surface, §6).
    Parity is with the Memory API, not the core tool signatures.
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # ADD-only spine: writes supersede, never erase
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    async def memory_add(
        namespace: _NS,
        text: Annotated[
            str,
            Field(
                description=(
                    "Natural-language text to remember (a message, note, or "
                    "observation). It is distilled into discrete facts, not "
                    "stored verbatim."
                )
            ),
        ],
        source: Annotated[
            str,
            Field(
                description=(
                    "Provenance label recorded on the stored episode and on the "
                    "console 'add' event, e.g. 'user', 'agent', 'import'. "
                    "Free-form; defaults to 'user'."
                )
            ),
        ] = "user",
        t_ref: Annotated[
            int | None,
            Field(
                description=(
                    "Reference time for this text, in epoch MILLISECONDS — the "
                    "moment its facts are anchored to and ranked for recency by. "
                    "Omit (null) to use the current wall-clock time; set it when "
                    "ingesting older material so its recency is not inflated."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Distill durable facts from text and write them to a namespace's memory.

        The raw text is NOT stored verbatim: an extraction pass distills it into
        discrete facts, which are embedded and indexed for memory_search. Returns
        the new fact ids and how many prior facts they superseded. Additive only —
        nothing is overwritten or deleted; a contradiction supersedes the older
        fact and the full history is retained. Being the console's OBSERVING
        wrapper, every call also records an 'add' event (timing, fact ids,
        supersessions) in the console event log — that is what makes the write
        visible in the console UI.

        Use it after learning durable information worth recalling in later
        sessions (preferences, decisions, biographical facts) — not for transient
        chatter, and not to re-state what memory already holds (check first with
        memory_search). This surface has no deletion tool by design.
        """
        res = await gateway.add(namespace, text, source=source, t_ref=t_ref)
        return {
            "fact_ids": res.fact_ids,
            "superseded_count": res.superseded_count,
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,  # never adds, supersedes or deletes a fact
            openWorldHint=False,
        )
    )
    async def memory_search(
        namespace: _NS,
        query: Annotated[
            str,
            Field(
                description=(
                    "Natural-language search query; matched against stored facts "
                    "by hybrid vector + full-text retrieval, then reranked."
                )
            ),
        ],
        k: Annotated[
            int,
            Field(
                description="Maximum number of facts to return (top-k after reranking)."
            ),
        ] = 5,
    ) -> dict[str, Any]:
        """Retrieve the facts most relevant to a query from a namespace's memory.

        Read-only with respect to memory: it never adds, supersedes or deletes a
        fact (searching a namespace that does not exist yet returns no hits,
        though the empty store file is created as a side effect). Always
        latest-only — superseded facts are never returned (the latest_only flag is
        REST-only, §6). Returns up to k hits, each with its fact text and final
        score, most relevant first. Being the console's observing wrapper, every
        call also records a 'search' event (query, k, hits, timing) in the console
        event log; stored memory itself is unchanged.

        Use it before answering anything that may depend on prior context —
        preferences, past decisions, earlier sessions. Facts only exist here if
        something wrote them via memory_add.
        """
        res = await gateway.search(
            namespace, query, k=k, latest_only=True, origin="agent"
        )
        return {
            "hits": [
                {"fact_text": h["fact_text"], "final_score": h["final_score"]}
                for h in res.hits
            ]
        }


def register_maintenance_tools(mcp: MCPServerType, gateway: EngineGateway) -> None:
    """Register the four §6.3 maintenance tools on `mcp`, routed through `gateway`.

    Identical names/signatures to the core stdio server: memory_maintenance_run
    (dry-run default), memory_maintenance_status, memory_review_queue,
    memory_review_decide.
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,  # apply=True claims the lease and writes
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    async def memory_maintenance_run(
        namespace: _NS,
        apply: Annotated[
            bool,
            Field(
                description=(
                    "False (default): dry-run — compute the full would-do report "
                    "with zero writes. True: claim the maintenance lease, apply "
                    "the provably-safe auto band, and stage judgment-call "
                    "proposals for review via memory_review_queue."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Run one sleep-time maintenance pass over a namespace (§6.3).

        DRY-RUN by default (apply=False): computes the would-do report with ZERO
        writes — no ledger row, no proposals. apply=True runs the provably-safe auto
        band and stages judgment-call proposals for human review. Symmetric with the
        CLI. NOTE: the LM_MAINT_AUTO auto-spawn path runs `--apply --auto-only` (auto
        band only, no proposals) — only interactive apply=True grows the review queue.
        Returns the run summary.

        Use it when memory_maintenance_status says a pass is due: run it first with
        apply=False to see what it would do, then with apply=True, then walk the staged
        proposals with memory_review_queue. It holds the namespace's lock for the whole
        run, so a concurrent memory_add / memory_search on that namespace waits for it.
        """
        return await gateway.maintain(namespace, apply=apply)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,  # pure ledger read: no writes, no model build
            openWorldHint=False,
        )
    )
    async def memory_maintenance_status(namespace: _NS) -> dict[str, Any]:
        """Report the namespace's maintenance ledger — runs + pending proposals (§6.3).

        A pure model-free ledger read, shaped identically to the core server's status
        tool ({namespace, runs, pending_proposals, last_run}). It never writes and
        never builds the embedder/reranker, so asking "when did maintenance last run?"
        stays cheap.

        Use it to see whether maintenance is due (then memory_maintenance_run) and how
        many proposals wait for a human (then memory_review_queue). It is the cheapest
        of the maintenance tools — prefer it for polling.
        """
        return await gateway.maintenance_status(namespace)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,  # listing lazily EXPIRES overdue proposals — a write
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def memory_review_queue(
        namespace: _NS,
        kind: Annotated[
            str | None,
            Field(
                description=(
                    "Filter to one proposal kind: 'dedup_near' | 'summarize' | "
                    "'evict'. Omit (null) for all kinds."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(description="Maximum number of proposals returned."),
        ] = 20,
    ) -> dict[str, Any]:
        """List pending maintenance proposals, grouped by entity, with evidence (§6.3).

        `kind` filters to 'dedup_near' | 'summarize' | 'evict'. Each proposal includes
        its parsed evidence payload — the before/after texts, cosine for near-dups,
        source facts for summaries, value signals for evictions — so the reviewer sees
        exactly what would change. NOT fully read-only: overdue proposals lazily expire
        (are marked expired) as a side effect of listing.

        Use it after memory_maintenance_run(apply=True) staged proposals, or whenever
        memory_maintenance_status reports pending ones. Show the evidence to the user
        and record their explicit verdicts with memory_review_decide — never decide on
        the user's behalf.
        """
        groups = await gateway.review_queue(namespace, kind=kind, limit=limit)
        # Round-trip through JSON so numpy/None edge values serialize cleanly, matching
        # the core server's json.dumps(..., default=str) shape.
        return json.loads(json.dumps({"groups": groups}, default=str))

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # ADD-only spine: approvals supersede, never erase
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    async def memory_review_decide(
        namespace: _NS,
        proposal_id: Annotated[
            str,
            Field(
                description=(
                    "ID of a pending proposal, as listed by memory_review_queue."
                )
            ),
        ],
        decision: Annotated[
            str,
            Field(
                description=(
                    "One of 'approve' | 'reject' | 'edit' | 'promote'. 'edit' is "
                    "valid only for summarize proposals; 'promote' only for evict "
                    "proposals."
                )
            ),
        ],
        edited_text: Annotated[
            str | None,
            Field(
                description=(
                    "Human-edited replacement summary text. Required when "
                    "decision='edit'; ignored otherwise."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Decide a maintenance proposal: approve | reject | edit | promote (§6.3).

        approve applies at decide-time with apply-time re-validation; reject leaves the
        spine byte-identical; edit (summarize only) approves the human-edited text;
        promote (evict only) rejects the eviction and lifts the fact to the hot tier.
        Never destructive — the spine is ADD-only, so an approval supersedes rather
        than erases.

        Use it once per proposal listed by memory_review_queue, and ONLY for a verdict
        the user stated explicitly: silence is not consent, and unreviewed proposals
        expire on their own.
        """
        result = await gateway.decide(
            namespace, proposal_id, decision, edited_text=edited_text
        )
        return json.loads(json.dumps(result, default=str))


def register_review_prompt(mcp: MCPServerType) -> None:
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
