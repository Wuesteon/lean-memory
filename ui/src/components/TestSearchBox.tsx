import { useState } from "react";
import { testSearch } from "../api";
import type { SearchHit } from "../types";
import ScoreTable from "./ScoreTable";

export default function TestSearchBox({
  ns,
  onRan,
}: {
  ns: string;
  onRan: () => void;
}) {
  const [query, setQuery] = useState("");
  const [k, setK] = useState(5);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setRunning(true);
    setError(null);
    try {
      const res = await testSearch(ns, query.trim(), k);
      setHits(res.hits);
      setDuration(res.duration_ms);
      onRan();
    } catch (err) {
      setError(String(err));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
      <div>
        <h2 className="text-sm font-semibold">Test search</h2>
        <p className="text-xs text-amber-600">
          Runs a real search — updates access stats (touch()).
        </p>
      </div>
      <form className="flex flex-wrap items-end gap-3" onSubmit={run}>
        <input
          className="min-w-[16rem] flex-1 rounded border border-slate-300 px-3 py-2 text-sm"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="query"
        />
        <label className="flex flex-col text-xs text-slate-500">
          k
          <input
            type="number"
            min="1"
            max="50"
            className="mt-1 w-16 rounded border border-slate-300 px-2 py-1 text-sm"
            value={k}
            onChange={(e) => setK(Math.max(1, Number(e.target.value)))}
          />
        </label>
        <button
          type="submit"
          disabled={running || query.trim() === ""}
          className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-40"
        >
          {running ? "Searching…" : "Search"}
        </button>
      </form>
      {error && <p className="text-xs text-red-600">{error}</p>}
      {hits && (
        <div>
          <div className="mb-1 text-xs text-slate-400">
            {hits.length} hits · {duration?.toFixed(0)} ms
          </div>
          <ScoreTable hits={hits} />
        </div>
      )}
    </div>
  );
}
