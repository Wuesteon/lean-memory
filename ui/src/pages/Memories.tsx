import { useCallback, useEffect, useState } from "react";
import { listFacts, listNamespaces } from "../api";
import type { Fact, TopPredicate } from "../types";
import type { FactFilters } from "../api";
import Pagination from "../components/Pagination";
import FactDrawer from "../components/FactDrawer";
import { formatTs } from "../lib/format";

const PAGE_SIZE = 50;

interface FilterState {
  latest_only: boolean;
  predicate: string;
  entity: string;
  min_salience: string;
  q: string;
}

const EMPTY: FilterState = {
  latest_only: true,
  predicate: "",
  entity: "",
  min_salience: "",
  q: "",
};

export default function Memories({ ns }: { ns: string }) {
  const [filters, setFilters] = useState<FilterState>(EMPTY);
  const [applied, setApplied] = useState<FilterState>(EMPTY);
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<Fact[]>([]);
  const [total, setTotal] = useState(0);
  const [predicates, setPredicates] = useState<TopPredicate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [openFact, setOpenFact] = useState<string | null>(null);

  useEffect(() => {
    // reset when the namespace changes
    setFilters(EMPTY);
    setApplied(EMPTY);
    setPage(1);
  }, [ns]);

  useEffect(() => {
    if (!ns) return;
    let cancelled = false;
    listNamespaces()
      .then((all) => {
        if (cancelled) return;
        const card = all.find((c) => c.name === ns);
        setPredicates(card ? card.top_predicates : []);
      })
      .catch(() => setPredicates([]));
    return () => {
      cancelled = true;
    };
  }, [ns]);

  const load = useCallback(() => {
    if (!ns) return;
    setLoading(true);
    setError(null);
    const params: FactFilters = {
      latest_only: applied.latest_only,
      predicate: applied.predicate || undefined,
      entity: applied.entity || undefined,
      min_salience:
        applied.min_salience === "" ? undefined : Number(applied.min_salience),
      q: applied.q || undefined,
      page,
      page_size: PAGE_SIZE,
    };
    listFacts(ns, params)
      .then((env) => {
        setRows(env.items);
        setTotal(env.total);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [ns, applied, page]);

  useEffect(() => {
    load();
  }, [load]);

  function applyFilters() {
    setApplied(filters);
    setPage(1);
  }

  if (!ns) {
    return <div className="p-6 text-sm text-slate-500">No namespace selected.</div>;
  }

  return (
    <div className="space-y-4 p-6">
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-200 bg-white p-4">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={filters.latest_only}
            onChange={(e) =>
              setFilters((f) => ({ ...f, latest_only: e.target.checked }))
            }
          />
          latest only
        </label>
        <label className="flex flex-col text-xs text-slate-500">
          predicate
          <select
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            value={filters.predicate}
            onChange={(e) =>
              setFilters((f) => ({ ...f, predicate: e.target.value }))
            }
          >
            <option value="">any</option>
            {predicates.map((p) => (
              <option key={p.predicate} value={p.predicate}>
                {p.predicate}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-xs text-slate-500">
          entity
          <input
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            value={filters.entity}
            onChange={(e) =>
              setFilters((f) => ({ ...f, entity: e.target.value }))
            }
            placeholder="name"
          />
        </label>
        <label className="flex flex-col text-xs text-slate-500">
          min salience
          <input
            type="number"
            step="0.1"
            min="0"
            max="10"
            className="mt-1 w-24 rounded border border-slate-300 px-2 py-1 text-sm"
            value={filters.min_salience}
            onChange={(e) =>
              setFilters((f) => ({ ...f, min_salience: e.target.value }))
            }
          />
        </label>
        <label className="flex flex-col text-xs text-slate-500">
          text (FTS)
          <input
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            value={filters.q}
            onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
            placeholder="match text"
          />
        </label>
        <button
          className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white"
          onClick={applyFilters}
        >
          Apply
        </button>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs text-slate-500">
            <tr>
              <th className="px-3 py-2">fact_text</th>
              <th className="px-3 py-2">subject</th>
              <th className="px-3 py-2">predicate</th>
              <th className="px-3 py-2">object</th>
              <th className="px-3 py-2 text-right">salience</th>
              <th className="px-3 py-2 text-right">conf</th>
              <th className="px-3 py-2">latest</th>
              <th className="px-3 py-2 text-right">access</th>
              <th className="px-3 py-2">valid_at</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.id}
                className="cursor-pointer border-t border-slate-100 hover:bg-slate-50"
                onClick={() => setOpenFact(r.id)}
              >
                <td className="max-w-xs truncate px-3 py-2">{r.fact_text}</td>
                <td className="px-3 py-2">{r.subject ?? "—"}</td>
                <td className="px-3 py-2">{r.predicate}</td>
                <td className="px-3 py-2">{r.object_literal ?? "—"}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {r.salience.toFixed(1)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {r.confidence.toFixed(2)}
                </td>
                <td className="px-3 py-2">
                  {r.is_latest ? (
                    <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-700">
                      yes
                    </span>
                  ) : (
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                      no
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {r.access_count}
                </td>
                <td className="px-3 py-2 text-xs text-slate-500">
                  {formatTs(r.valid_at)}
                </td>
              </tr>
            ))}
            {rows.length === 0 && !loading && (
              <tr>
                <td colSpan={9} className="px-3 py-6 text-center text-slate-400">
                  no facts match these filters
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <Pagination
        page={page}
        pageSize={PAGE_SIZE}
        total={total}
        onPage={setPage}
      />

      {openFact && (
        <FactDrawer ns={ns} factId={openFact} onClose={() => setOpenFact(null)} />
      )}
    </div>
  );
}
