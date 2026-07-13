import type { NamespaceCard, WhoAmI } from "../types";
import Sparkline from "../components/Sparkline";
import ConnectSnippets from "../components/ConnectSnippets";
import {
  factsPerAdd,
  formatBytes,
  formatCount,
  formatPct,
  formatTs,
  supersessionRate,
} from "../lib/format";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}

function Card({ ns }: { ns: NamespaceCard }) {
  const rate = supersessionRate(ns.facts_latest, ns.facts_retired);
  const fpa = factsPerAdd(ns.facts_latest, ns.facts_retired, ns.activity.adds);
  return (
    <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-5">
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold">{ns.name}</h3>
        <span className="text-xs text-slate-400">{formatBytes(ns.file_size)}</span>
      </div>

      <div className="grid grid-cols-5 gap-3">
        <Stat label="facts (latest)" value={formatCount(ns.facts_latest)} />
        <Stat label="retired" value={formatCount(ns.facts_retired)} />
        <Stat label="chains" value={formatCount(ns.chains)} />
        <Stat label="episodes" value={formatCount(ns.episodes)} />
        <Stat label="entities" value={formatCount(ns.entities)} />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <Stat label="supersession rate" value={formatPct(rate)} />
        <Stat
          label="facts / add"
          value={fpa === null ? "—" : fpa.toFixed(1)}
        />
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">
          top predicates
        </div>
        <div className="flex flex-wrap gap-1">
          {ns.top_predicates.length === 0 && (
            <span className="text-xs text-slate-400">none</span>
          )}
          {ns.top_predicates.map((p) => (
            <span
              key={p.predicate}
              className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700"
            >
              {p.predicate} · {p.count}
            </span>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between text-xs text-slate-600">
          <span>activity (7d)</span>
          <span className="text-slate-400">
            {ns.activity.adds} adds · {ns.activity.searches} searches
          </span>
        </div>
        <Sparkline adds={ns.activity.adds} searches={ns.activity.searches} />
        {ns.activity.earliest_ts !== null && (
          <div className="mt-1 text-[11px] text-slate-400">
            events retained from {formatTs(ns.activity.earliest_ts)} (older
            events pruned at the 10k cap)
          </div>
        )}
      </div>
    </div>
  );
}

export default function Overview({
  who,
  namespaces,
}: {
  who: WhoAmI;
  namespaces: NamespaceCard[];
}) {
  if (namespaces.length === 0) {
    return (
      <div className="p-6">
        <ConnectSnippets mode={who.mode} dataRoot={who.data_root} />
      </div>
    );
  }
  return (
    <div className="grid gap-4 p-6 md:grid-cols-2">
      {namespaces.map((ns) => (
        <Card key={ns.name} ns={ns} />
      ))}
    </div>
  );
}
