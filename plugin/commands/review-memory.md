---
description: Review staged sleep-time memory-maintenance proposals — you record only the decisions the user explicitly makes.
---

Maintenance runs offline and STAGES judgment calls (near-duplicate merges,
summaries, evictions) as *proposals*; nothing changes in memory until a human
approves. Walk the user through the queue and record only their explicit
verdicts.

**Hard rule — you may NOT decide any proposal on your own.** Every
approve / reject / edit / promote MUST come from an explicit user verdict in
this conversation. If the user has not ruled on an item, leave it pending. A
batch verb ("approve all exact dedups", "reject every eviction") is valid ONLY
when the user says it in those words — never infer a batch decision from a
single example or from silence. Silence is not consent; unreviewed proposals
expire on their own.

Ask the user which namespace to review if they have not said. Then:

1. **Fetch the queue.** Call the `memory_review_queue` MCP tool
   (`namespace=<ns>`, optional `kind` in `dedup_near|summarize|evict`,
   `limit`). It returns proposals grouped by subject entity, each carrying its
   evidence payload.

2. **Present, batched by entity and kind, with evidence.** Show the before/after
   texts, cosine for near-dups, source facts for summaries, and the value
   signals for evictions. Keep it scannable so the user can rule quickly.

3. **Collect explicit verdicts.** Ask when a verdict is missing or ambiguous.
   Do not proceed on an item the user has not decided.

4. **Decide per item the user ruled on.** Call `memory_review_decide`
   (`namespace`, `proposal_id`, `decision`, optional `edited_text`) with
   `decision` in `approve|reject|edit|promote`. `edited_text` applies only to a
   summarize proposal the user chose to reword before approving. `promote` is
   for lifting an eviction's fact back to the hot tier.

5. **Summarize.** Report what was applied, what was rejected, and what remains
   pending (untouched, still awaiting the user).

Notes for the user:
- Approving is the only thing that changes stored memory. Rejecting leaves the
  spine byte-identical; ignoring a proposal lets it expire on its own.
- Use **one namespace per project/session** — the same guidance as the other
  memory commands.
