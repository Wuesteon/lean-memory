import { useState } from "react";
import type { AddPayload, EventRow, SearchPayload } from "../types";
import { formatTs } from "../lib/format";
import ScoreTable from "./ScoreTable";

function Badge({ text, tone }: { text: string; tone: "add" | "search" | "error" }) {
  const cls =
    tone === "error"
      ? "bg-red-100 text-red-700"
      : tone === "add"
        ? "bg-indigo-100 text-indigo-700"
        : "bg-sky-100 text-sky-700";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>
      {text}
    </span>
  );
}

function AddRow({ payload }: { payload: AddPayload }) {
  return (
    <div className="space-y-2 text-xs">
      <div>
        <span className="text-slate-500">created:</span>{" "}
        {payload.fact_ids.length === 0 ? (
          <span className="text-slate-400">none</span>
        ) : (
          <span className="break-all font-mono">
            {payload.fact_ids.join(", ")}
          </span>
        )}
      </div>
      <div>
        <span className="text-slate-500">superseded:</span>{" "}
        {payload.superseded_fact_ids.length === 0 ? (
          <span className="text-slate-400">none</span>
        ) : (
          <span className="break-all font-mono">
            {payload.superseded_fact_ids.join(", ")}
          </span>
        )}
      </div>
      <div className="text-slate-400">
        source {payload.source} · {payload.episode_text_chars} chars ·{" "}
        {payload.fact_count} facts
      </div>
    </div>
  );
}

function Row({ event }: { event: EventRow }) {
  const [open, setOpen] = useState(false);
  const errored = Boolean(event.payload.error);
  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <button
        className="flex w-full items-center gap-3 px-4 py-2 text-left"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-slate-400">{open ? "▾" : "▸"}</span>
        <Badge text={event.kind} tone={event.kind} />
        {errored && <Badge text="error" tone="error" />}
        <span className="text-xs text-slate-500">{formatTs(event.ts)}</span>
        <span className="ml-auto text-xs tabular-nums text-slate-400">
          {event.duration_ms.toFixed(0)} ms
        </span>
      </button>
      {open && (
        <div className="border-t border-slate-100 px-4 py-3">
          {errored && (
            <p className="mb-2 text-xs text-red-600">
              {event.payload.error}
            </p>
          )}
          {event.kind === "add" ? (
            <AddRow payload={event.payload as AddPayload} />
          ) : (
            <div className="space-y-2">
              <div className="text-xs text-slate-500">
                query: “{(event.payload as SearchPayload).query}” · k=
                {(event.payload as SearchPayload).k} · origin{" "}
                {(event.payload as SearchPayload).origin}
              </div>
              <ScoreTable hits={(event.payload as SearchPayload).hits} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function EventFeed({ events }: { events: EventRow[] }) {
  return (
    <div className="space-y-2">
      {events.map((e) => (
        <Row key={e.id} event={e} />
      ))}
    </div>
  );
}
