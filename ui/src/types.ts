export type Mode = "local" | "docker";
export type AuthKind = "token" | "bearer";
export type ModelsMode = "real" | "stub";

export interface WhoAmI {
  mode: Mode;
  auth: AuthKind;
  authenticated: boolean;
  data_root: string;
  // Resolved retrieval-backend mode. "stub" means semantic scores are
  // deterministic offline placeholders (spec §11 banner).
  models: ModelsMode;
}

export interface Envelope<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

export interface TopPredicate {
  predicate: string;
  count: number;
}

export interface NamespaceActivity {
  adds: number;
  searches: number;
  earliest_ts: number | null;
}

export interface NamespaceCard {
  name: string;
  facts_latest: number;
  facts_retired: number;
  entities: number;
  episodes: number;
  chains: number;
  file_size: number;
  top_predicates: TopPredicate[];
  activity: NamespaceActivity;
}

export interface Fact {
  // Raw DB rows serialize the primary key as `id`; only test-search hits use
  // `fact_id` (the gateway renames it — see SearchHit).
  id: string;
  fact_text: string;
  subject: string | null;
  predicate: string;
  object_literal: string | null;
  salience: number;
  confidence: number;
  is_latest: number;
  access_count: number;
  valid_at: number;
  valid_to: number | null;
  superseded_by: string | null;
  episode_id: string;
  created_at: number;
}

export interface ChainLink {
  id: string;
  fact_text: string;
  valid_at: number;
  valid_to: number | null;
  is_latest: number;
}

export interface EpisodeRef {
  id: string;
  raw: string;
  source: string | null;
  t_ref: number;
}

export interface FactDetail extends Fact {
  chain: ChainLink[];
  episode: EpisodeRef | null;
}

export interface Episode {
  id: string;
  raw: string;
  source: string | null;
  t_ref: number;
  created_at: number;
}

export interface EpisodeDetail extends Episode {
  facts: Fact[];
}

export interface Entity {
  name: string;
  fact_count: number;
}

export interface SearchHit {
  fact_id: string;
  fact_text: string;
  final_score: number;
  relevance: number;
  recency: number;
  importance: number;
  dense_rank: number | null;
  sparse_rank: number | null;
  rrf_score: number | null;
}

export interface AddPayload {
  episode_text_chars: number;
  source: string;
  t_ref: number | null;
  fact_ids: string[];
  fact_count: number;
  superseded_fact_ids: string[];
  superseded_count: number;
  origin?: string;
  error?: string;
}

export interface SearchPayload {
  query: string;
  k: number;
  latest_only: boolean;
  origin: string;
  hits: SearchHit[];
  error?: string;
}

export interface EventRow {
  id: number;
  namespace: string;
  ts: number;
  kind: "add" | "search";
  duration_ms: number;
  payload: AddPayload | SearchPayload;
}

// ── Maintenance review (spec §8.1) ──────────────────────────────────────────
export type ProposalKind = "dedup_near" | "summarize" | "evict";

// Per-kind evidence payloads, mirroring the transforms' staged JSON verbatim
// (transforms.py dedup_near/summarize/evict). fact_texts/source_fact_texts are
// id→text maps.
export interface DedupNearPayload {
  slot: { subject_id: string; predicate: string };
  fact_ids: string[];
  fact_texts: Record<string, string>;
  cosine: number;
  multivalued: boolean;
  proposed_survivor: string;
  evidence_backend: string;
}

export interface SummarizePayload {
  subject_id: string;
  source_fact_ids: string[];
  source_fact_texts: Record<string, string>;
  summary_text: string;
  evidence_backend: string;
}

export interface EvictPayload {
  fact_id: string;
  fact_text: string;
  value: number;
  salience: number;
  access_count: number;
  evidence_backend: string;
}

// One `maintenance_proposal` row (SELECT *), plus the parsed `payload` the
// gateway's review_queue attaches per proposal (memory.py review_queue).
export interface Proposal {
  id: string;
  run_id: string;
  namespace: string;
  kind: ProposalKind;
  payload_json: string;
  status: string;
  expiry_reason: string | null;
  created_at: number;
  expires_at: number;
  decided_at: number | null;
  decided_by: string | null;
  applied_at: number | null;
  edited_text: string | null;
  evidence_backend: string | null;
  payload: DedupNearPayload | SummarizePayload | EvictPayload;
}

// review_queue returns one group per subject entity (memory.py review_queue).
export interface ProposalGroup {
  entity_id: string | null;
  entity_name: string | null;
  proposals: Proposal[];
}

// The ledger-only status read (maintain/mcp_support.py read_status).
export interface MaintenanceRun {
  id: string;
  status: string;
  started_at: number;
  finished_at: number | null;
  trigger: string;
}

export interface MaintenanceStatus {
  namespace: string;
  runs: number;
  pending_proposals: number;
  last_run: MaintenanceRun | null;
}

// The run summary returned by maintenance/run (cli._report_to_dict via the
// gateway). Only the fields the page surfaces are typed.
export interface MaintenanceRunResult {
  namespace: string;
  status: string; // 'ok' | 'skipped'
  mode: string; // 'dry-run' | 'apply' | 'auto-only'
  skipped_reason: string | null;
  below_threshold: boolean;
  merges: number;
  demoted: number;
  staged: number;
  dropped_proposals: number;
}

// The decide/promote lifecycle result (lifecycle.py). `outcome` drives the UI;
// remaining fields vary by outcome and are read opportunistically.
export interface DecideResult {
  outcome: string;
  proposal_id?: string;
  fact_id?: string;
  status?: string;
  applied_at?: number | null;
  tier?: string;
}
