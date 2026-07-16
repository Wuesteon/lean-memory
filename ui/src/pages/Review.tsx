import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import {
  reviewQueue,
  decideProposal,
  promoteFact,
  maintenanceStatus,
  runMaintenance,
} from "../api";
import type {
  ProposalGroup,
  Proposal,
  MaintenanceStatus,
  DedupNearPayload,
  SummarizePayload,
  EvictPayload,
} from "../types";
import { formatTs } from "../lib/format";

const KIND_LABEL: Record<string, string> = {
  dedup_near: "near-duplicate",
  summarize: "summarize",
  evict: "evict",
};

export default function Review({ ns }: { ns: string }) {
  const [status, setStatus] = useState<MaintenanceStatus | null>(null);
  const [groups, setGroups] = useState<ProposalGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // Collapsed entity ids (default expanded). Keyed by entity_id ?? "" for the
  // no-subject group.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // Maintenance-run state: "run" is the dry-run button; the apply variant is
  // gated behind a confirm step (like TestSearchBox's submitting pattern).
  const [running, setRunning] = useState(false);
  const [confirmApply, setConfirmApply] = useState(false);

  // Monotonic request token: every reload() invalidates all in-flight ones,
  // including imperative reloads fired by mutation handlers — so a slow
  // response for a superseded namespace can never write state (the effect's
  // `cancelled` flag alone would only cover the effect-driven load).
  const reloadSeq = useRef(0);

  // Deliberately does NOT clear `error`: mutation handlers set an error and
  // then refresh to DB truth, and the message must survive that refresh.
  function reload() {
    if (!ns) return;
    const seq = ++reloadSeq.current;
    setLoading(true);
    Promise.all([maintenanceStatus(ns), reviewQueue(ns)])
      .then(([st, gs]) => {
        if (seq !== reloadSeq.current) return;
        setStatus(st);
        setGroups(gs);
      })
      .catch((e) => {
        if (seq === reloadSeq.current) setError(String(e));
      })
      .finally(() => {
        if (seq === reloadSeq.current) setLoading(false);
      });
  }

  useEffect(() => {
    setCollapsed(new Set());
    setNotice(null);
    setError(null);
    setConfirmApply(false);
    reload();
    return () => {
      reloadSeq.current++;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ns]);

  // Optimistically drop a proposal from its group after a decision; prune a
  // now-empty group.
  function removeProposal(proposalId: string) {
    setGroups((gs) =>
      gs
        .map((g) => ({
          ...g,
          proposals: g.proposals.filter((p) => p.id !== proposalId),
        }))
        .filter((g) => g.proposals.length > 0),
    );
  }

  // Lifecycle outcomes that mean the decision landed as asked; anything else
  // on a 200 (expired / not_found / invalid_decision) must not be silently
  // treated as success.
  const DECIDED = new Set(["applied", "rejected", "promoted"]);

  async function decide(
    p: Proposal,
    decision: "approve" | "reject" | "edit",
    editedText?: string,
  ) {
    setNotice(null);
    // Optimistic removal — restored via a full refresh on a 409/anomaly.
    removeProposal(p.id);
    try {
      const res = await decideProposal(ns, p.id, decision, editedText);
      if (!DECIDED.has(res.outcome)) {
        setNotice(`Proposal ${res.outcome.replace("_", " ")} — refreshing.`);
        reload();
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const prior = (e.body as { status?: string } | undefined)?.status;
        setNotice(
          `Proposal already ${prior ?? "decided"} elsewhere — refreshing.`,
        );
        reload();
        return;
      }
      setError(String(e));
      reload();
    }
  }

  async function approveGroup(g: ProposalGroup) {
    setNotice(null);
    const ids = g.proposals.map((p) => p.id);
    for (const id of ids) removeProposal(id);
    let conflicts = 0;
    let anomalies = 0;
    for (const id of ids) {
      try {
        const res = await decideProposal(ns, id, "approve");
        if (!DECIDED.has(res.outcome)) anomalies++;
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          conflicts++;
          continue;
        }
        setError(String(e));
      }
    }
    if (conflicts || anomalies) {
      const parts = [];
      if (conflicts) parts.push(`${conflicts} already decided elsewhere`);
      if (anomalies) parts.push(`${anomalies} not applied (expired/invalid)`);
      setNotice(`Batch approve: ${parts.join(", ")} — refreshing.`);
    }
    reload();
  }

  async function promote(factId: string, proposalId: string) {
    setNotice(null);
    removeProposal(proposalId);
    try {
      const res = await promoteFact(ns, factId);
      if (res.outcome !== "promoted") {
        setNotice(`Promote: ${res.outcome.replace("_", " ")} — refreshing.`);
        reload();
      }
    } catch (e) {
      setError(String(e));
      reload();
    }
  }

  async function doRun(apply: boolean) {
    setRunning(true);
    setError(null);
    setNotice(null);
    setConfirmApply(false);
    try {
      const res = await runMaintenance(ns, apply);
      const verb = apply ? "Applied" : "Dry-run";
      setNotice(
        `${verb}: staged ${res.staged}, merges ${res.merges}, demoted ${res.demoted}` +
          (res.below_threshold ? " (below threshold)" : ""),
      );
      reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  }

  function toggle(id: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (!ns) {
    return <div className="p-6 text-sm text-slate-500">No namespace selected.</div>;
  }

  return (
    <div className="space-y-4 p-6">
      <div className="flex flex-wrap items-center gap-4 rounded-lg border border-slate-200 bg-white p-4">
        <div className="text-sm">
          <div className="font-semibold">Maintenance</div>
          <div className="text-xs text-slate-500">
            {status ? (
              <>
                {status.pending_proposals} pending ·{" "}
                {status.last_run
                  ? `last run ${status.last_run.status} at ${formatTs(
                      status.last_run.finished_at ?? status.last_run.started_at,
                    )}`
                  : "never run"}
              </>
            ) : (
              "…"
            )}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-40"
            disabled={running}
            onClick={() => doRun(false)}
          >
            {running ? "Running…" : "Run maintenance (dry-run)"}
          </button>
          {confirmApply ? (
            <>
              <button
                className="rounded bg-red-600 px-3 py-1.5 text-sm text-white disabled:opacity-40"
                disabled={running}
                onClick={() => doRun(true)}
              >
                Confirm apply
              </button>
              <button
                className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-600"
                onClick={() => setConfirmApply(false)}
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 disabled:opacity-40"
              disabled={running}
              onClick={() => setConfirmApply(true)}
            >
              Apply…
            </button>
          )}
        </div>
      </div>

      {notice && (
        <p className="rounded bg-amber-50 px-3 py-2 text-xs text-amber-700">{notice}</p>
      )}
      {error && <p className="text-sm text-red-600">{error}</p>}

      {groups.length === 0 && !loading && (
        <div className="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-400">
          No pending proposals
        </div>
      )}

      {groups.map((g) => {
        const key = g.entity_id ?? "";
        const isCollapsed = collapsed.has(key);
        return (
          <div
            key={key}
            className="rounded-lg border border-slate-200 bg-white"
          >
            <div className="flex items-center gap-3 border-b border-slate-100 px-4 py-3">
              <button
                className="text-xs text-slate-400 hover:text-slate-700"
                onClick={() => toggle(key)}
              >
                {isCollapsed ? "▸" : "▾"}
              </button>
              <div className="text-sm font-semibold">
                {g.entity_name ?? g.entity_id ?? "(no subject)"}
              </div>
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                {g.proposals.length}
              </span>
              <button
                className="ml-auto rounded bg-emerald-600 px-2.5 py-1 text-xs text-white"
                onClick={() => approveGroup(g)}
              >
                Approve all
              </button>
            </div>
            {!isCollapsed && (
              <div className="divide-y divide-slate-100">
                {g.proposals.map((p) => (
                  <ProposalCard
                    key={p.id}
                    p={p}
                    onDecide={decide}
                    onPromote={promote}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function KindBadge({ kind }: { kind: string }) {
  return (
    <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-700">
      {KIND_LABEL[kind] ?? kind}
    </span>
  );
}

function ProposalCard({
  p,
  onDecide,
  onPromote,
}: {
  p: Proposal;
  onDecide: (
    p: Proposal,
    decision: "approve" | "reject" | "edit",
    editedText?: string,
  ) => void;
  onPromote: (factId: string, proposalId: string) => void;
}) {
  // Prefill the editor with the proposed summary text (edit-then-approve is
  // only offered for summarize).
  const initialEdit =
    p.kind === "summarize" ? (p.payload as SummarizePayload).summary_text : "";
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(initialEdit);

  return (
    <div className="space-y-3 px-4 py-3">
      <div className="flex items-center gap-2">
        <KindBadge kind={p.kind} />
        {p.evidence_backend && (
          <span className="text-[10px] text-slate-400">{p.evidence_backend}</span>
        )}
      </div>

      {p.kind === "dedup_near" && (
        <DedupEvidence payload={p.payload as DedupNearPayload} />
      )}
      {p.kind === "summarize" && (
        <SummarizeEvidence payload={p.payload as SummarizePayload} />
      )}
      {p.kind === "evict" && (
        <EvictEvidence payload={p.payload as EvictPayload} />
      )}

      {editing && (
        <textarea
          className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
          rows={3}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
      )}

      <div className="flex flex-wrap gap-2">
        {editing ? (
          <>
            <button
              className="rounded bg-emerald-600 px-2.5 py-1 text-xs text-white disabled:opacity-40"
              disabled={draft.trim() === ""}
              onClick={() => onDecide(p, "edit", draft)}
            >
              Save & approve
            </button>
            <button
              className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600"
              onClick={() => setEditing(false)}
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              className="rounded bg-emerald-600 px-2.5 py-1 text-xs text-white"
              onClick={() => onDecide(p, "approve")}
            >
              Approve
            </button>
            <button
              className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600"
              onClick={() => onDecide(p, "reject")}
            >
              Keep
            </button>
            {p.kind === "summarize" && (
              <button
                className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600"
                onClick={() => setEditing(true)}
              >
                Edit-then-approve
              </button>
            )}
            {p.kind === "evict" && (
              <button
                className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600"
                onClick={() =>
                  onPromote((p.payload as EvictPayload).fact_id, p.id)
                }
              >
                Promote
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function DedupEvidence({ payload }: { payload: DedupNearPayload }) {
  const [a, b] = payload.fact_ids;
  return (
    <div className="space-y-1">
      <div className="grid gap-2 sm:grid-cols-2">
        <FactBox
          text={payload.fact_texts[a]}
          highlight={payload.proposed_survivor === a}
        />
        <FactBox
          text={payload.fact_texts[b]}
          highlight={payload.proposed_survivor === b}
        />
      </div>
      <div className="text-[10px] text-slate-400">
        cosine {payload.cosine.toFixed(4)}
        {payload.multivalued && " · multivalued slot"} · survivor is highlighted
      </div>
    </div>
  );
}

function SummarizeEvidence({ payload }: { payload: SummarizePayload }) {
  return (
    <div className="space-y-2">
      <div>
        <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-400">
          {payload.source_fact_ids.length} sources
        </div>
        <ul className="space-y-1">
          {payload.source_fact_ids.map((id) => (
            <li
              key={id}
              className="rounded bg-slate-50 px-2 py-1 text-xs text-slate-600"
            >
              {payload.source_fact_texts[id]}
            </li>
          ))}
        </ul>
      </div>
      <div>
        <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-400">
          proposed summary
        </div>
        <div className="rounded border border-emerald-200 bg-emerald-50 px-2 py-1 text-sm text-emerald-800">
          {payload.summary_text}
        </div>
      </div>
    </div>
  );
}

function EvictEvidence({ payload }: { payload: EvictPayload }) {
  return (
    <div className="space-y-1">
      <FactBox text={payload.fact_text} />
      <div className="text-[10px] text-slate-400">
        value {payload.value.toFixed(4)} · salience {payload.salience.toFixed(1)} ·
        access {payload.access_count}
      </div>
    </div>
  );
}

function FactBox({
  text,
  highlight,
}: {
  text: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={`rounded border px-2 py-1 text-sm ${
        highlight
          ? "border-emerald-300 bg-emerald-50 text-emerald-800"
          : "border-slate-200 bg-slate-50 text-slate-700"
      }`}
    >
      {text}
    </div>
  );
}
