"""Engine flaw found by Phase 2 ingest at scale: the known-entities list handed
to the router/typer grew unboundedly with namespace age (~5 entities/turn on
conversational data), inflating the constrained-typing prompt until it silently
truncated. The cap keeps the most recent names."""

from lean_memory.memory import _KNOWN_ENTITIES_CAP, Memory
from lean_memory.types import Entity


def test_known_entity_names_capped_to_most_recent(tmp_path):
    mem = Memory(root=tmp_path)
    store = mem._store("ns")
    for i in range(_KNOWN_ENTITIES_CAP + 50):
        store.upsert_entity(Entity(namespace="ns", name=f"person {i}", type=None))
    known = mem._known_entity_names(store, "ns")
    assert len(known) == _KNOWN_ENTITIES_CAP
    assert f"person {_KNOWN_ENTITIES_CAP + 49}" in known  # newest kept
    assert "person 0" not in known  # oldest dropped
    mem.close()
