import { useEffect, useState } from "react";
import { getFact } from "../api";
import type { FactDetail } from "../types";
import { formatTs } from "../lib/format";

function interval(validAt: number, validTo: number | null): string {
  return `${formatTs(validAt)} → ${validTo === null ? "now" : formatTs(validTo)}`;
}

export default function FactDrawer({
  ns,
  factId,
  onClose,
}: {
  ns: string;
  factId: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<FactDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    getFact(ns, factId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [ns, factId]);

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div
        className="absolute inset-0 bg-black/30"
        onClick={onClose}
        aria-hidden
      />
      <div className="relative z-50 h-full w-full max-w-lg overflow-y-auto bg-white p-6 shadow-xl">
        <div className="flex items-start justify-between">
          <h2 className="text-sm font-semibold">Fact detail</h2>
          <button
            className="rounded border border-slate-300 px-2 py-1 text-xs"
            onClick={onClose}
          >
            Close
          </button>
        </div>

        {error && <p className="mt-4 text-sm text-red-600">{error}</p>}
        {!detail && !error && (
          <p className="mt-4 text-sm text-slate-400">Loading…</p>
        )}

        {detail && (
          <div className="mt-4 space-y-6">
            <p className="text-sm">{detail.fact_text}</p>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
              <dt className="text-slate-500">subject</dt>
              <dd>{detail.subject ?? "—"}</dd>
              <dt className="text-slate-500">predicate</dt>
              <dd>{detail.predicate}</dd>
              <dt className="text-slate-500">object</dt>
              <dd>{detail.object_literal ?? "—"}</dd>
              <dt className="text-slate-500">salience</dt>
              <dd className="tabular-nums">{detail.salience.toFixed(2)}</dd>
              <dt className="text-slate-500">confidence</dt>
              <dd className="tabular-nums">{detail.confidence.toFixed(2)}</dd>
              <dt className="text-slate-500">access count</dt>
              <dd className="tabular-nums">{detail.access_count}</dd>
              <dt className="text-slate-500">is latest</dt>
              <dd>{detail.is_latest ? "yes" : "no"}</dd>
              <dt className="text-slate-500">valid</dt>
              <dd>{interval(detail.valid_at, detail.valid_to)}</dd>
            </dl>

            <div>
              <h3 className="mb-2 text-xs font-semibold text-slate-600">
                Supersession timeline
              </h3>
              <ol className="space-y-3 border-l-2 border-slate-200 pl-4">
                {detail.chain.map((link) => (
                  <li key={link.id} className="relative">
                    <span
                      className={`absolute -left-[21px] top-1 h-3 w-3 rounded-full border-2 ${
                        link.id === detail.id
                          ? "border-slate-900 bg-slate-900"
                          : "border-slate-300 bg-white"
                      }`}
                    />
                    <div className="flex items-center gap-2">
                      <span className="text-xs">{link.fact_text}</span>
                      {link.is_latest === 1 && (
                        <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                          latest
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-slate-400">
                      {interval(link.valid_at, link.valid_to)}
                    </div>
                  </li>
                ))}
                {detail.chain.length === 0 && (
                  <li className="text-xs text-slate-400">
                    no supersession chain (standalone fact)
                  </li>
                )}
              </ol>
            </div>

            <div>
              <h3 className="mb-2 text-xs font-semibold text-slate-600">
                Provenance episode
              </h3>
              {detail.episode ? (
                <div className="rounded border border-slate-200 bg-slate-50 p-3">
                  <div className="text-[11px] text-slate-400">
                    {detail.episode.source ?? "unknown"} ·{" "}
                    {formatTs(detail.episode.t_ref)}
                  </div>
                  <p className="mt-1 whitespace-pre-wrap text-xs">
                    {detail.episode.raw}
                  </p>
                </div>
              ) : (
                <p className="text-xs text-slate-400">episode unavailable</p>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
