"""WP2 — offline tests for the `--arm` surface (the mem0 comparison arm).

Everything here runs with mem0 ABSENT and without network: the adapter is
driven against a stub module that mimics mem0 2.x OSS (including its refusal
of the Platform-only temporal parameters), and the missing-dependency path is
exercised by making the real import fail. Bench import follows the
test_phase2_* precedent (bench/ is not part of the installed package).
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

import update_integrity as ui  # noqa: E402
from update_integrity import (  # noqa: E402
    SCENARIOS,
    AssertionResult,
    Mem0Arm,
    Mem0Config,
    assertion_names,
    emit,
    main,
    run_scenario,
)

TEMPORAL_REFUSAL = "Platform-only temporal parameter. Not supported in OSS."


class _FakeMem0Client:
    """Stand-in for `mem0.Memory`: add-only, no supersession, OSS temporal rules."""

    def __init__(self, config):
        self.config = config
        self._rows: list[dict] = []

    # mem0 2.x signatures
    def add(self, messages, *, user_id=None, timestamp=None, **kwargs):
        if timestamp is not None:
            raise ValueError(f"add(timestamp): {TEMPORAL_REFUSAL}")
        row = {"id": f"m{len(self._rows)}", "memory": str(messages),
               "user_id": user_id, "event": "ADD"}
        self._rows.append(row)
        return {"results": [dict(row)]}

    def search(self, query, *, filters=None, top_k=20, reference_date=None, **kwargs):
        if reference_date is not None:
            raise ValueError(f"search(reference_date): {TEMPORAL_REFUSAL}")
        user_id = (filters or {}).get("user_id")
        hits = [r for r in self._rows if r["user_id"] == user_id][:top_k]
        return {"results": [dict(h) for h in hits]}

    def get_all(self, *, filters=None, top_k=20, **kwargs):
        return self.search("", filters=filters, top_k=top_k)

    def history(self, memory_id):
        return [{"memory_id": memory_id, "event": "ADD",
                 "old_memory": None, "new_memory": ""}]


class _FakeMem0Module:
    __version__ = "9.9.9-fake"

    class Memory:
        @staticmethod
        def from_config(config):
            return _FakeMem0Client(config)


def _fake_arm(**kwargs):
    return Mem0Arm(_FakeMem0Module(), timeout=0, **kwargs)


# --- arm flag parsing -------------------------------------------------------


def test_default_arm_is_lean_memory_and_the_flag_is_a_no_op(tmp_path, capsys):
    rc_default = main(["--markdown", "--root", str(tmp_path / "a")])
    default_out = capsys.readouterr().out
    rc_explicit = main(["--arm", "lean-memory", "--markdown", "--root", str(tmp_path / "b")])
    explicit_out = capsys.readouterr().out

    assert rc_default == rc_explicit == 0
    assert default_out == explicit_out
    assert default_out.startswith("# Update-integrity results — lean-memory ")
    assert "n/a (unsupported)" not in default_out


def test_unknown_arm_is_rejected():
    with pytest.raises(SystemExit) as excinfo:
        main(["--arm", "nope"])
    assert excinfo.value.code == 2


# --- missing dependency -----------------------------------------------------


def test_mem0_arm_without_mem0_exits_2_with_the_install_hint(monkeypatch, capsys):
    real_import_module = importlib.import_module

    def _no_mem0(name, *args, **kwargs):
        if name == "mem0":
            raise ImportError("No module named 'mem0'")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _no_mem0)

    rc = main(["--arm", "mem0", "--markdown"])
    captured = capsys.readouterr()

    assert rc == 2, "a missing mem0 must be a hard error, never a silent skip"
    assert "pip install mem0ai" in captured.err
    assert captured.out == "", "no results table may be emitted without mem0"


def test_install_hint_names_the_pypi_package():
    assert "pip install mem0ai" in ui.MEM0_INSTALL_HINT


# --- cell-for-cell comparability -------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_lean_memory_arm_emits_the_contracted_assertion_rows(scenario, tmp_path):
    assert [r.name for r in run_scenario(scenario, tmp_path)] == assertion_names(scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_mem0_arm_emits_the_same_assertion_rows(scenario, tmp_path):
    results = _fake_arm().run_scenario(scenario, tmp_path)
    assert [r.name for r in results] == assertion_names(scenario)


def test_mem0_arm_marks_as_of_unsupported_and_quotes_the_refusal(tmp_path):
    scenario = next(s for s in SCENARIOS if s.key == "employer_change")
    results = _fake_arm().run_scenario(scenario, tmp_path)
    as_of = next(r for r in results if r.name == "as-of-returns-old-truth")

    assert as_of.supported is False
    assert TEMPORAL_REFUSAL in as_of.detail
    assert "add(timestamp=…)" in as_of.detail
    # graded assertions are unaffected by the unsupported one
    assert all(r.supported for r in results if r.name != "as-of-returns-old-truth")


def test_mem0_arm_falls_back_when_add_rejects_the_step_timestamp(tmp_path):
    arm = _fake_arm()
    arm.run_scenario(next(s for s in SCENARIOS if s.key == "city_move"), tmp_path)
    assert arm._timestamp_supported is False
    assert TEMPORAL_REFUSAL in (arm._timestamp_error or "")


def test_mem0_arm_scenario_error_keeps_the_table_aligned(tmp_path):
    class _Exploding(_FakeMem0Module):
        class Memory:
            @staticmethod
            def from_config(config):
                raise RuntimeError("ollama is down")

    scenario = next(s for s in SCENARIOS if s.key == "employer_change")
    results = Mem0Arm(_Exploding(), timeout=0).run_scenario(scenario, tmp_path)

    assert [r.name for r in results] == assertion_names(scenario)
    assert all(not r.ok for r in results)
    assert "ollama is down" in results[0].detail


# --- header pins the backend ------------------------------------------------


def test_mem0_header_pins_version_and_full_backend():
    header = _fake_arm().header("3.13.7")

    assert "mem0 9.9.9-fake" in header
    assert "llm=ollama/qwen2.5:3b" in header
    assert "embedder=ollama/nomic-embed-text (768d)" in header
    assert "vector_store=qdrant (local, on-disk)" in header
    assert "ollama_base_url=http://localhost:11434" in header
    assert "Python 3.13.7" in header
    assert "n/a (unsupported)" in header


def test_mem0_memory_config_is_local_only(tmp_path):
    config = _fake_arm().memory_config(tmp_path, "employer_change")

    assert config["llm"] == {"provider": "ollama",
                             "config": {"model": "qwen2.5:3b", "temperature": 0.0,
                                        "ollama_base_url": "http://localhost:11434"}}
    assert config["embedder"]["config"]["model"] == "nomic-embed-text"
    assert config["vector_store"]["config"]["path"].startswith(str(tmp_path))
    assert config["history_db_path"].startswith(str(tmp_path))


def test_mem0_config_flags_reach_the_header():
    arm = Mem0Arm(_FakeMem0Module(),
                  Mem0Config(llm_model="llama3.2:1b", embedder_model="mxbai-embed-large",
                             embedding_dims=1024),
                  timeout=0)
    assert "llm=ollama/llama3.2:1b" in arm.header("3.13.7")
    assert "embedder=ollama/mxbai-embed-large (1024d)" in arm.header("3.13.7")


# --- table semantics --------------------------------------------------------


def test_emit_renders_na_rows_and_excludes_them_from_the_tally(capsys):
    rows = [("employer_change", [
        AssertionResult("top1-is-current", True),
        AssertionResult("as-of-returns-old-truth", False, "no equivalent", supported=False),
    ])]

    all_ok = emit(rows, "# header", markdown=True)
    out = capsys.readouterr().out

    assert all_ok is True
    assert "| employer_change | as-of-returns-old-truth | n/a (unsupported) | no equivalent |" in out
    assert "**ALL PASS** — 1/1 assertions." in out
    assert "1 further assertion(s) rendered `n/a (unsupported)`" in out


def test_emit_reports_failures_and_keeps_na_separate(capsys):
    rows = [("city_move", [
        AssertionResult("top1-is-current", False, "expected 'Munich'"),
        AssertionResult("as-of-returns-old-truth", False, "no equivalent", supported=False),
    ])]

    all_ok = emit(rows, "# header", markdown=True)
    out = capsys.readouterr().out

    assert all_ok is False
    assert "**FAILURES PRESENT** — 0/1 assertions." in out
    assert "| city_move | top1-is-current | FAIL | expected 'Munich' |" in out


def test_emit_plain_mode_labels_na_rows(capsys):
    rows = [("city_move", [AssertionResult("as-of-returns-old-truth", False,
                                           "no equivalent", supported=False)])]
    emit(rows, "# header", markdown=False)
    assert "n/a   no equivalent" in capsys.readouterr().out


# --- shared matcher ---------------------------------------------------------


def test_contains_is_case_insensitive_for_every_arm():
    assert ui._contains("work", "Works at Acme")
    assert ui._contains("Zorbex", "works at zorbex")
    assert not ui._contains("Acme", "Works at Zorbex")
