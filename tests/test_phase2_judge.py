import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from phase2_ingest import Question
from phase2_judge import (
    EvalConfig, LME_TEMPLATES, LOCOMO_LENIENT_PROMPT, StubJudge,
    config_hash, lme_anscheck_prompt, parse_correct_label, preprocess_locomo_gold,
)


def test_lme_ku_template_verbatim():
    # Golden: must match evaluate_qa.py character-for-character.
    assert LME_TEMPLATES["knowledge-update"] == (
        "I will give you a question, a correct answer, and a response from a "
        "model. Please answer yes if the response contains the correct answer. "
        "Otherwise, answer no. If the response contains some previous "
        "information along with an updated answer, the response should be "
        "considered as correct as long as the updated answer is the required "
        "answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: "
        "{}\n\nIs the model response correct? Answer yes or no only."
    )


def test_lme_prompt_dispatch():
    p = lme_anscheck_prompt("multi-session", "Q", "A", "R", abstention=False)
    assert p.startswith("I will give you a question, a correct answer,")
    assert "Question: Q" in p and "Correct Answer: A" in p and "Model Response: R" in p
    pa = lme_anscheck_prompt("multi-session", "Q", "A", "R", abstention=True)
    assert pa.startswith("I will give you an unanswerable question,")


def test_locomo_lenient_contains_load_bearing_rules():
    assert "PARTIAL CREDIT" in LOCOMO_LENIENT_PROMPT
    assert "DATE TOLERANCE" in LOCOMO_LENIENT_PROMPT
    assert "{question}" in LOCOMO_LENIENT_PROMPT


def test_preprocess_locomo_gold_cat3_semicolon():
    assert preprocess_locomo_gold(3, "The Lakers; maybe the Celtics") == "The Lakers"
    assert preprocess_locomo_gold(2, "8 May, 2023; ish") == "8 May, 2023; ish"


def test_parse_correct_label():
    assert parse_correct_label('{"reasoning": "x", "label": "CORRECT"}') is True
    assert parse_correct_label('{"reasoning": "x", "label": "WRONG"}') is False
    assert parse_correct_label("label: CORRECT") is True
    import pytest
    from phase2_judge import JudgeParseError
    with pytest.raises(JudgeParseError):
        parse_correct_label("CORRECT or WRONG, who knows")


def test_stub_judge_substring():
    q = Question(question_id="x", question_type="knowledge-update", question="Q", gold="Quandril")
    assert StubJudge().grade(q, "They work at Quandril now.") is True
    assert StubJudge().grade(q, "They work at Zorbex.") is False


def test_config_hash_stable():
    cfg = EvalConfig(
        benchmark="longmemeval", slice="ku", dataset_file="x.json", dataset_sha256="abc",
        judge_id="lme-official", judge_model="openai/gpt-4o-2024-08-06", judge_prompt="P",
        backbone_model="openai/gpt-4.1-mini", provider="openrouter", k=10,
        is_latest_only=True, reader_prompt="R", embedder_model="E", reranker_model="RR",
        generator_model="G", typer_model="T", retrieval_constants="{}", git_commit="deadbeef",
    )
    h1, h2 = config_hash(cfg), config_hash(cfg)
    assert h1 == h2 and len(h1) == 64
