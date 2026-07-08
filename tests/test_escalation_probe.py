"""Offline test for the escalation probe: inject a duck-typed fake GLiNER2 model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))

from phase2_escalation_probe import run_probe  # noqa: E402

from lean_memory.extract.gliner_extractor import Gliner2Generator  # noqa: E402


class FakeGliner2:
    """Duck-types gliner2's extract_entities/extract_relations return shapes."""

    def extract_entities(self, text, entity_types, **kw):
        return {"person": [{"text": "Alice", "confidence": 0.9, "start": 0, "end": 5}]}

    def extract_relations(self, text, relation_types, **kw):
        return {
            "relation_extraction": {
                "works_at": [
                    {
                        "head": {"text": "Alice", "confidence": 0.9, "start": 0, "end": 5},
                        "tail": {"text": "Acme", "confidence": 0.8, "start": 15, "end": 19},
                    }
                ]
            }
        }


def test_run_probe_offline_shape_and_determinism():
    gen = Gliner2Generator(model=FakeGliner2())
    namespaces = [["Alice works at Acme.", "Alice works at Acme."]]
    r1 = run_probe(namespaces, typing_threshold=0.5, conf_threshold=0.5, generator=gen)
    r2 = run_probe(namespaces, typing_threshold=0.5, conf_threshold=0.5, generator=gen)
    assert r1 == r2  # deterministic
    assert r1["seen"] == 2  # FakeGliner2 emits 1 candidate/turn × 2 turns
    assert 0.0 <= r1["rate"] <= 1.0
    # subset, not equality: Task 5 adds shape-metric keys to this dict
    assert {"typing_threshold", "conf_threshold", "seen", "escalated", "rate", "by_reason"}.issubset(r1)


import json


def test_json_flag_writes_results(tmp_path):
    from phase2_escalation_probe import write_json

    out = tmp_path / "probe.json"
    results = [{"typing_threshold": 0.5, "conf_threshold": 0.5, "seen": 2,
                "escalated": 1, "rate": 0.5, "by_reason": {"coreference": 1}}]
    write_json(out, slice_="ku", n_namespaces=1, total_turns=2, results=results)
    data = json.loads(out.read_text())
    assert data["slice"] == "ku"
    assert data["results"][0]["rate"] == 0.5
