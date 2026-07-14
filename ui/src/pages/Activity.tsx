import { useCallback, useEffect, useRef, useState } from "react";
import { listEvents, listNamespaces } from "../api";
import type { EventRow, ModelsMode } from "../types";
import EventFeed from "../components/EventFeed";
import TestSearchBox from "../components/TestSearchBox";
import StubBanner from "../components/StubBanner";

const POLL_MS = 4000;
const PAGE_SIZE = 50;

type KindFilter = "all" | "add" | "search";

export default function Activity({
  ns,
  models,
}: {
  ns: string;
  models: ModelsMode;
}) {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [kind, setKind] = useState<KindFilter>("all");
  const [error, setError] = useState<string | null>(null);
  const [hasSidecar, setHasSidecar] = useState<boolean | null>(null);
  const loadedOnce = useRef(false);

  const fetchEvents = useCallback(
    (isCancelled?: () => boolean) => {
      if (!ns) return;
      const k = kind === "all" ? undefined : kind;
      listEvents(ns, k, 1, PAGE_SIZE)
        .then((env) => {
          if (isCancelled?.()) return;
          setEvents(env.items);
          setError(null);
          loadedOnce.current = true;
        })
        .catch((e) => {
          if (isCancelled?.()) return;
          setError(String(e));
        });
    },
    [ns, kind],
  );

  // reset per-namespace state so events never bleed across namespaces
  useEffect(() => {
    setEvents([]);
    setHasSidecar(null);
    loadedOnce.current = false;
  }, [ns]);

  // read earliest_ts for the active namespace to decide the sidecar hint
  useEffect(() => {
    if (!ns) return;
    let cancelled = false;
    listNamespaces()
      .then((all) => {
        if (cancelled) return;
        const card = all.find((c) => c.name === ns);
        setHasSidecar(card ? card.activity.earliest_ts !== null : null);
      })
      .catch(() => {
        if (!cancelled) setHasSidecar(null);
      });
    return () => {
      cancelled = true;
    };
  }, [ns, events.length]);

  useEffect(() => {
    let cancelled = false;
    const isCancelled = () => cancelled;
    loadedOnce.current = false;
    fetchEvents(isCancelled);
    const id = window.setInterval(() => {
      if (document.hidden) return;
      fetchEvents(isCancelled);
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [fetchEvents]);

  if (!ns) {
    return (
      <>
        <StubBanner models={models} />
        <div className="p-6 text-sm text-slate-500">No namespace selected.</div>
      </>
    );
  }

  const missingSidecar =
    loadedOnce.current && events.length === 0 && hasSidecar === false;

  return (
    <>
      <StubBanner models={models} />
      <div className="space-y-4 p-6">
        <TestSearchBox ns={ns} onRan={() => fetchEvents()} />

      <div className="flex items-center gap-3">
        <span className="text-xs text-slate-500">filter</span>
        {(["all", "add", "search"] as KindFilter[]).map((k) => (
          <button
            key={k}
            className={`rounded px-2 py-1 text-xs ${
              kind === k
                ? "bg-slate-900 text-white"
                : "border border-slate-300 text-slate-600"
            }`}
            onClick={() => setKind(k)}
          >
            {k}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-slate-400">
          polling every {POLL_MS / 1000}s
        </span>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      {missingSidecar ? (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          No event traces for this namespace. Connect via the observing MCP
          (<code>uvx lean-memory-console mcp</code>) so adds and searches are
          captured — the core stdio server writes memories but no{" "}
          <code>_events.db</code> sidecar.
        </div>
      ) : (
        <EventFeed events={events} />
      )}

      {!missingSidecar && events.length === 0 && !error && (
        <p className="text-sm text-slate-400">no events yet</p>
      )}
      </div>
    </>
  );
}
