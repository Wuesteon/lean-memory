import type {
  WhoAmI,
  Envelope,
  NamespaceCard,
  Fact,
  FactDetail,
  Episode,
  EpisodeDetail,
  Entity,
  EventRow,
  SearchHit,
  ProposalGroup,
  MaintenanceStatus,
  MaintenanceRunResult,
  DecideResult,
} from "./types";

export class ApiError extends Error {
  status: number;
  /** Parsed JSON error body when the server sent one — e.g. the review
   *  routes' 409 pass-through carries {outcome, status, applied_at}. */
  body?: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

let sessionToken: string | null = null;
let bearerKey: string | null = null;

/** Local mode: pull ?token from the URL, keep it in module memory, and strip
 *  it from the address bar so it never leaks via Referer or bookmarks. */
export function bootLocalToken(): void {
  const params = new URLSearchParams(window.location.search);
  const t = params.get("token");
  if (t) {
    sessionToken = t;
    params.delete("token");
    const qs = params.toString();
    const clean = window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash;
    window.history.replaceState(null, "", clean);
  }
}

/** Docker mode: store the key entered on the login screen (React context only). */
export function setBearerKey(key: string): void {
  bearerKey = key;
}

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = {};
  if (sessionToken) h["X-Console-Token"] = sessionToken;
  if (bearerKey) h["Authorization"] = `Bearer ${bearerKey}`;
  return h;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    let body: unknown;
    try {
      body = await res.json();
      const b = body as { detail?: string; message?: string } | null;
      detail = (b && (b.detail || b.message)) || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail, body);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function qp(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

// ── §7 endpoints ────────────────────────────────────────────────────────

export function whoami(): Promise<WhoAmI> {
  return req<WhoAmI>("/views/whoami");
}

export function listNamespaces(): Promise<NamespaceCard[]> {
  return req<NamespaceCard[]>("/views/namespaces");
}

export interface FactFilters {
  latest_only?: boolean;
  predicate?: string;
  entity?: string;
  min_salience?: number;
  q?: string;
  page?: number;
  page_size?: number;
}

export function listFacts(ns: string, f: FactFilters = {}): Promise<Envelope<Fact>> {
  return req<Envelope<Fact>>(`/views/${encodeURIComponent(ns)}/facts${qp({ ...f })}`);
}

export function getFact(ns: string, factId: string): Promise<FactDetail> {
  return req<FactDetail>(
    `/views/${encodeURIComponent(ns)}/facts/${encodeURIComponent(factId)}`,
  );
}

export function listEpisodes(
  ns: string,
  page = 1,
  pageSize = 50,
): Promise<Envelope<Episode>> {
  return req<Envelope<Episode>>(
    `/views/${encodeURIComponent(ns)}/episodes${qp({ page, page_size: pageSize })}`,
  );
}

export function getEpisode(ns: string, episodeId: string): Promise<EpisodeDetail> {
  return req<EpisodeDetail>(
    `/views/${encodeURIComponent(ns)}/episodes/${encodeURIComponent(episodeId)}`,
  );
}

export function listEntities(
  ns: string,
  page = 1,
  pageSize = 50,
): Promise<Envelope<Entity>> {
  return req<Envelope<Entity>>(
    `/views/${encodeURIComponent(ns)}/entities${qp({ page, page_size: pageSize })}`,
  );
}

export function listEvents(
  ns: string,
  kind?: "add" | "search",
  page = 1,
  pageSize = 50,
): Promise<Envelope<EventRow>> {
  return req<Envelope<EventRow>>(
    `/views/${encodeURIComponent(ns)}/events${qp({ kind, page, page_size: pageSize })}`,
  );
}

export function testSearch(
  ns: string,
  query: string,
  k: number,
): Promise<{ hits: SearchHit[]; duration_ms: number }> {
  return req<{ hits: SearchHit[]; duration_ms: number }>(
    `/views/${encodeURIComponent(ns)}/test-search`,
    { method: "POST", body: JSON.stringify({ query, k }) },
  );
}

// ── §8.1 maintenance review endpoints ──────────────────────────────────────

export function reviewQueue(
  ns: string,
  kind?: string,
  limit?: number,
): Promise<ProposalGroup[]> {
  return req<ProposalGroup[]>(
    `/views/${encodeURIComponent(ns)}/review/queue${qp({ kind, limit })}`,
  );
}

// A re-decided proposal comes back as a 409 (routes/review.py) — `req` raises
// ApiError(409); the caller refreshes the queue on that status.
export function decideProposal(
  ns: string,
  proposalId: string,
  decision: "approve" | "reject" | "edit",
  editedText?: string,
): Promise<DecideResult> {
  return req<DecideResult>(
    `/views/${encodeURIComponent(ns)}/review/${encodeURIComponent(proposalId)}/decide`,
    { method: "POST", body: JSON.stringify({ decision, edited_text: editedText }) },
  );
}

export function promoteFact(ns: string, factId: string): Promise<DecideResult> {
  return req<DecideResult>(
    `/views/${encodeURIComponent(ns)}/review/promote`,
    { method: "POST", body: JSON.stringify({ fact_id: factId }) },
  );
}

export function maintenanceStatus(ns: string): Promise<MaintenanceStatus> {
  return req<MaintenanceStatus>(
    `/views/${encodeURIComponent(ns)}/maintenance/status`,
  );
}

export function runMaintenance(
  ns: string,
  apply: boolean,
): Promise<MaintenanceRunResult> {
  return req<MaintenanceRunResult>(
    `/views/${encodeURIComponent(ns)}/maintenance/run`,
    { method: "POST", body: JSON.stringify({ apply }) },
  );
}
