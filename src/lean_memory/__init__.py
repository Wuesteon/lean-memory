"""lean-memory — embedded, local-first agent-memory engine (Phase 0 spine).

Public API:
    from lean_memory import Memory
    mem = Memory(root="./data")
    mem.add("user-42", "I work at Acme.")
    hits = mem.search("user-42", "where does the user work?")
"""

from .memory import Memory
from .types import Entity, Episode, Fact, RetrievedFact

__all__ = ["Memory", "Episode", "Entity", "Fact", "RetrievedFact"]
__version__ = "0.0.1"
