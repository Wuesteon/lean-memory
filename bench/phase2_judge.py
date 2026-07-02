"""Phase 2 frozen judges. Three judges, all pinned; EvalConfig sha256 is the
run's identity. The three disputed variables (judge_model, judge_prompt,
backbone_model) live verbatim in every EvalConfig.

Verbatim transcriptions:
  LME_TEMPLATES   ← github.com/xiaowu0162/LongMemEval src/evaluation/evaluate_qa.py
  LOCOMO_LENIENT  ← github.com/mem0ai/memory-benchmarks benchmarks/locomo/prompts.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from phase2_ingest import Question  # noqa: E402
from phase2_reader import openrouter_chat  # noqa: E402


class JudgeParseError(RuntimeError):
    """Judge output had no unambiguous label — abort, never silently score 0."""


# ── LongMemEval official templates (verbatim) ──

_LME_DEFAULT = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."

LME_TEMPLATES = {
    "default": _LME_DEFAULT,
    "temporal-reasoning": "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "knowledge-update": "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "single-session-preference": "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "abstention": "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only.",
}


def lme_anscheck_prompt(qtype: str, question: str, answer: str, response: str,
                        abstention: bool = False) -> str:
    if abstention:
        template = LME_TEMPLATES["abstention"]
    elif qtype in ("single-session-user", "single-session-assistant", "multi-session"):
        template = LME_TEMPLATES["default"]
    elif qtype in ("temporal-reasoning", "knowledge-update", "single-session-preference"):
        template = LME_TEMPLATES[qtype]
    else:
        raise NotImplementedError(qtype)
    return template.format(question, answer, response)


# ── LoCoMo lenient judge (Mem0 memory-benchmarks, verbatim; no-evidence build) ──

LOCOMO_JUDGE_SYSTEM_PROMPT = "You are evaluating conversational AI memory recall. Return JSON only with the format requested."

LOCOMO_LENIENT_PROMPT = """Label the generated answer as CORRECT or WRONG.

## Rules

1. **PARTIAL CREDIT**: If the generated answer includes AT LEAST ONE correct item from the gold answer's list, mark CORRECT. Getting 1 out of 2, 2 out of 4, etc. is always acceptable. Only mark WRONG if NONE of the gold answer items appear.

2. **PARAPHRASES COUNT**: Same concept in different words is CORRECT. "Chocolate raspberry tart" = "chocolate cake with raspberries". "Shelter meal service" = "volunteering at a homeless shelter". Emotions and sentiments in the same positive/negative family count as paraphrases: "proud" = "fulfilled" = "accomplished"; "huge success" = "relieved" = "thrilled" (all express positive achievement). Judge semantic meaning, not exact wording.

3. **EXTRA DETAIL IS FINE**: A longer answer that includes the gold answer's key facts plus additional information is CORRECT. Never penalize for being more detailed or specific. If the generated answer adds extra descriptive details beyond the gold answer while still referencing the same core entity or concept, mark CORRECT.

4. **DATE TOLERANCE**: Dates within 14 days of each other are CORRECT. Durations within 50% are CORRECT (e.g., "5 months" matches "six months"; "19 days" matches "two weeks"). Relative dates ("few days before November") match specific dates in the same window. A specific date (e.g., "February 2020") that is consistent with a vague reference (e.g., "a few years ago" relative to 2023) is CORRECT. Converting "last year" to the actual year (e.g., "2022" when conversations are in 2023) is CORRECT.

5. **SEMANTIC OVERLAP**: Judge whether the generated answer addresses the same topic and captures the core idea of the gold answer. Different wording, phrasing, or level of detail should not result in WRONG if the underlying concept matches. For EMOTIONS and FEELINGS questions, answers expressing sentiments in the same valence (positive/negative) about the same event are CORRECT — do not require the exact same emotion word.

6. **SAME REFERENT**: If the generated answer mentions or references the same named entity, character, person, or concept as the gold answer, mark CORRECT — even if the generated answer provides a different physical description or includes additional details. The key question is: does the generated answer identify the same core entity? If yes, it is CORRECT.

7. **FOCUS ON KNOWLEDGE, NOT WORDING**: The goal is to assess whether the system recalled the right fact. Minor differences in specificity, phrasing, or scope should not result in WRONG. Only mark WRONG when the generated answer demonstrates a genuinely different or incorrect understanding.

## ONLY mark WRONG if:
- The generated answer contains ZERO correct items from the gold answer
- The answer addresses a completely different topic

## Question
Question: {question}
Gold answer: {answer}
Generated answer: {response}

Return JSON with "reasoning" (one sentence) and "label" (CORRECT or WRONG). Do NOT include both labels."""


# ── LoCoMo strict judge (ours, frozen) ──

LOCOMO_STRICT_PROMPT = """Label the generated answer as CORRECT or WRONG.

You are a strict grader. Apply these rules exactly:

1. CORRECT only if the generated answer states the same value as the gold answer, or an unambiguous paraphrase of it. Generic or vague answers that merely overlap in topic are WRONG.
2. TEMPORAL PRECISION: For questions about dates or times, the answer must be point-in-time correct. Dates must match within 1 day. Durations must match within 1 of the stated unit (e.g. "18 days" accepts 17-19 days; "3 months" accepts 2-4 months). Anything looser is WRONG.
3. LIST ANSWERS: If the gold answer is a list, the generated answer must contain every item of the list to be CORRECT. A single matching item is not enough.
4. NO CREDIT FOR HEDGING: "I don't know", "not specified", or a refusal is WRONG whenever the gold answer contains a value. It is CORRECT only when the gold answer itself states that the question is unanswerable.
5. STALE VALUES: If the question asks for a current value and the generated answer gives only an earlier, superseded value, it is WRONG — even if that value was once true.

Question: {question}
Gold answer: {answer}
Generated answer: {response}

Return JSON with "reasoning" (one sentence) and "label" (CORRECT or WRONG). Do NOT include both labels."""


def preprocess_locomo_gold(category: Optional[int], answer: str) -> str:
    """Mem0 harness rule: category 3 gold truncated at the first ';'."""
    if category == 3 and ";" in answer:
        return answer.split(";")[0].strip()
    return answer


_LABEL_RE = re.compile(r'"label"\s*:\s*"(CORRECT|WRONG)"', re.IGNORECASE)


def parse_correct_label(out: str) -> bool:
    m = _LABEL_RE.search(out)
    if m:
        return m.group(1).upper() == "CORRECT"
    up = out.upper()
    has_c, has_w = "CORRECT" in up.replace("INCORRECT", ""), "WRONG" in up
    if has_c != has_w:
        return has_c
    raise JudgeParseError(f"ambiguous judge output: {out[:200]!r}")


# ── judge classes ──

class StubJudge:
    """Offline: case-insensitive substring of gold in hypothesis. Plumbing only."""

    judge_id = "stub"
    model = "stub"

    def prompt_repr(self) -> str:
        return "substring(gold, hypothesis)"

    def grade(self, q: Question, hypothesis: str) -> bool:
        return q.gold.lower() in hypothesis.lower()


class LMEOfficialJudge:
    judge_id = "lme-official"
    model = "openai/gpt-4o-2024-08-06"

    def prompt_repr(self) -> str:
        return json.dumps(LME_TEMPLATES, sort_keys=True)

    def grade(self, q: Question, hypothesis: str) -> bool:
        prompt = lme_anscheck_prompt(q.question_type, q.question, q.gold, hypothesis,
                                     abstention=q.is_abstention)
        out = openrouter_chat(self.model, [{"role": "user", "content": prompt}],
                              temperature=0.0, max_tokens=10)
        return "yes" in out.strip().lower()


class _LocomoJudge:
    prompt_template = ""  # subclass sets

    def prompt_repr(self) -> str:
        return self.prompt_template

    def grade(self, q: Question, hypothesis: str) -> bool:
        gold = preprocess_locomo_gold(q.category, q.gold)
        prompt = self.prompt_template.format(question=q.question, answer=gold,
                                             response=hypothesis)
        out = openrouter_chat(self.model, [
            {"role": "system", "content": LOCOMO_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], temperature=0.0, max_tokens=300)
        return parse_correct_label(out)


class LocomoLenientJudge(_LocomoJudge):
    judge_id = "locomo-lenient"
    model = "openai/gpt-4o-mini"
    prompt_template = LOCOMO_LENIENT_PROMPT


class LocomoStrictJudge(_LocomoJudge):
    judge_id = "locomo-strict"
    model = "openai/gpt-4o"
    prompt_template = LOCOMO_STRICT_PROMPT


# ── frozen config ──

from lean_memory.extract.contradiction import DEFAULT_HIGH_SIM, DEFAULT_LOW_SIM  # noqa: E402
from lean_memory.retrieve.retriever import (  # noqa: E402
    DECAY_LAMBDA, OVER_RETRIEVE, RRF_K, W_IMP, W_REC, W_REL,
)

RETRIEVAL_CONSTANTS = {
    "RRF_K": RRF_K, "OVER_RETRIEVE": OVER_RETRIEVE,
    "W_REL": W_REL, "W_REC": W_REC, "W_IMP": W_IMP,
    "DECAY_LAMBDA": DECAY_LAMBDA,
    "HIGH_SIM": DEFAULT_HIGH_SIM, "LOW_SIM": DEFAULT_LOW_SIM,
    "ROUTER_CONF_THRESHOLD": 0.5,
}


@dataclass(frozen=True)
class EvalConfig:
    benchmark: str
    slice: str
    dataset_file: str
    dataset_sha256: str
    judge_id: str
    judge_model: str
    judge_prompt: str
    backbone_model: str
    provider: str
    k: int
    is_latest_only: object  # True | False | "fc"
    reader_prompt: str
    embedder_model: str
    reranker_model: str
    generator_model: str
    typer_model: str
    retrieval_constants: str  # sorted-JSON string of RETRIEVAL_CONSTANTS
    git_commit: str


def config_hash(cfg: EvalConfig) -> str:
    payload = json.dumps(asdict(cfg), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
