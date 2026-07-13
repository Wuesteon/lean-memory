export type Mode = "local" | "docker";
export type AuthKind = "token" | "bearer";

export interface WhoAmI {
  mode: Mode;
  auth: AuthKind;
  authenticated: boolean;
  data_root: string;
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
  fact_id: string;
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
  fact_id: string;
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
