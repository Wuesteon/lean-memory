"""Sleep-time maintenance — config, scoring, summarizer seam, and transforms.

The heart of the offline maintenance job (design spec §3–§4): pure functions over
the Store ABC that deduplicate, summarize, and evict stored memory while preserving
the ADD-only spine and as-of semantics. Auto transforms apply provably-safe verbs;
propose transforms stage judgment calls into the human review queue.
"""

from __future__ import annotations

from .config import MS_PER_DAY, MaintenanceConfig
from .lifecycle import decide, promote_fact
from .score import value
from .summarize import (
    ExtractiveStubSummarizer,
    OllamaSummarizer,
    Summarizer,
    default_summarizer,
)
from .runner import MaintenanceRunner, RunReport, live_lease_is_fresh
from .transforms import (
    Merge,
    StagedProposal,
    TransformReport,
    dedup_exact,
    dedup_near,
    evict_auto,
    evict_propose,
    normalize_text,
    run_transforms,
    summarize,
)

__all__ = [
    "MaintenanceConfig",
    "MS_PER_DAY",
    "value",
    "decide",
    "promote_fact",
    "Summarizer",
    "ExtractiveStubSummarizer",
    "OllamaSummarizer",
    "default_summarizer",
    "normalize_text",
    "dedup_exact",
    "dedup_near",
    "summarize",
    "evict_propose",
    "evict_auto",
    "run_transforms",
    "Merge",
    "StagedProposal",
    "TransformReport",
    "MaintenanceRunner",
    "RunReport",
    "live_lease_is_fresh",
]
