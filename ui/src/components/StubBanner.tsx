import type { ModelsMode } from "../types";

/**
 * Slim advisory banner (spec §11): when the console runs on stub retrieval
 * backends, the semantic scores shown across the UI are deterministic offline
 * placeholders, not real embedder/reranker output. Renders nothing when the
 * resolved models mode is "real". Kept visually quiet — one thin strip.
 */
export default function StubBanner({ models }: { models: ModelsMode }) {
  if (models !== "stub") return null;
  return (
    <div className="border-b border-amber-200 bg-amber-50 px-6 py-1.5 text-[11px] text-amber-800">
      Running on stub retrieval backends — semantic scores are stub-generated
      (deterministic placeholders, not real embedder/reranker output). Install
      the <code>[models]</code> extra for real scores.
    </div>
  );
}
