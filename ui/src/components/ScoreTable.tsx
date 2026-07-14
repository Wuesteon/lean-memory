import type { SearchHit } from "../types";

function weighted(hit: SearchHit): number {
  return 0.6 * hit.relevance + 0.2 * hit.recency + 0.2 * hit.importance;
}

export default function ScoreTable({ hits }: { hits: SearchHit[] }) {
  if (hits.length === 0) {
    return <p className="text-xs text-slate-400">no hits</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="py-1 pr-3">fact</th>
            <th className="py-1 pr-3 text-right">final</th>
            <th className="py-1 pr-3 text-right">0.6·rel+0.2·rec+0.2·imp</th>
            <th className="py-1 pr-3 text-right">rel</th>
            <th className="py-1 pr-3 text-right">rec</th>
            <th className="py-1 pr-3 text-right">imp</th>
            <th className="py-1 pr-3 text-right">dense</th>
            <th className="py-1 pr-3 text-right">sparse</th>
            <th className="py-1 pr-3 text-right">rrf</th>
          </tr>
        </thead>
        <tbody>
          {hits.map((h) => (
            <tr key={h.fact_id} className="border-t border-slate-100">
              <td className="max-w-[16rem] truncate py-1 pr-3">{h.fact_text}</td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {h.final_score.toFixed(3)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums text-slate-500">
                {weighted(h).toFixed(3)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {h.relevance.toFixed(3)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {h.recency.toFixed(3)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {h.importance.toFixed(3)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {h.dense_rank ?? "—"}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {h.sparse_rank ?? "—"}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {h.rrf_score === null ? "—" : h.rrf_score.toFixed(3)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
