import { useEffect, useState } from "react";
import { listEpisodes } from "../api";
import type { Episode } from "../types";
import Pagination from "../components/Pagination";
import EpisodeRow from "../components/EpisodeRow";

const PAGE_SIZE = 50;

export default function Episodes({ ns }: { ns: string }) {
  const [rows, setRows] = useState<Episode[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setPage(1);
  }, [ns]);

  useEffect(() => {
    if (!ns) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    listEpisodes(ns, page, PAGE_SIZE)
      .then((env) => {
        if (cancelled) return;
        setRows(env.items);
        setTotal(env.total);
      })
      .catch((e) => !cancelled && setError(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [ns, page]);

  if (!ns) {
    return <div className="p-6 text-sm text-slate-500">No namespace selected.</div>;
  }

  return (
    <div className="space-y-4 p-6">
      {error && <p className="text-sm text-red-600">{error}</p>}
      {rows.length === 0 && !loading && !error && (
        <p className="text-sm text-slate-400">no episodes in this namespace</p>
      )}
      <div className="space-y-2">
        {rows.map((ep) => (
          <EpisodeRow key={ep.id} ns={ns} episode={ep} />
        ))}
      </div>
      <Pagination
        page={page}
        pageSize={PAGE_SIZE}
        total={total}
        onPage={setPage}
      />
    </div>
  );
}
