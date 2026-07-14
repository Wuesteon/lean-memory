import { useState } from "react";
import { getEpisode } from "../api";
import type { Episode, Fact } from "../types";
import { formatTs } from "../lib/format";

export default function EpisodeRow({
  ns,
  episode,
}: {
  ns: string;
  episode: Episode;
}) {
  const [open, setOpen] = useState(false);
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && facts === null && !loading) {
      setLoading(true);
      setError(null);
      getEpisode(ns, episode.id)
        .then((d) => setFacts(d.facts))
        .catch((e) => setError(String(e)))
        .finally(() => setLoading(false));
    }
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <button
        className="flex w-full items-start gap-3 px-4 py-3 text-left"
        onClick={toggle}
      >
        <span className="mt-0.5 text-slate-400">{open ? "▾" : "▸"}</span>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] text-slate-400">
            {episode.source ?? "unknown"} · {formatTs(episode.t_ref)}
          </div>
          <p
            className={`text-sm ${open ? "whitespace-pre-wrap" : "truncate"}`}
          >
            {episode.raw}
          </p>
        </div>
      </button>

      {open && (
        <div className="border-t border-slate-100 px-4 py-3">
          <div className="mb-2 text-xs font-semibold text-slate-600">
            Extracted facts
          </div>
          {loading && <p className="text-xs text-slate-400">Loading…</p>}
          {error && <p className="text-xs text-red-600">{error}</p>}
          {facts && facts.length === 0 && (
            <p className="text-xs text-slate-400">no facts extracted</p>
          )}
          {facts && facts.length > 0 && (
            <ul className="space-y-1">
              {facts.map((f) => (
                <li
                  key={f.id}
                  className="flex items-center gap-2 text-xs"
                >
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      f.is_latest ? "bg-emerald-500" : "bg-slate-300"
                    }`}
                  />
                  <span>{f.fact_text}</span>
                  <span className="text-slate-400">({f.predicate})</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
