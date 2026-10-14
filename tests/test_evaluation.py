"""Evaluation engine tests — heuristic and llm_judge, honest method labels."""

from __future__ import annotations

import json

import pytest

from teachonce.errors import LLMNotConfiguredError, SchemaValidationError
from teachonce.evaluation import SkillEvaluator
from teachonce.llm import ScriptedLLM
from teachonce.models import DecisionRule, Skill, SkillVersion


def _setup_two_versions(store) -> str:
    """v1: no recording rule. v2: adds a recording rule."""
    skill = Skill.create("课程资料整理")
    store.create_skill(skill)
    store.add_skill_version(
        SkillVersion(
            skill_id=skill.id,
            version=1,
            trigger_conditions=["整理课程资料时"],
            decision_rules=[
                DecisionRule.create(rule_id="r1", condition="归档时", decision="放考试资料/"),
                DecisionRule.create(rule_id="r2", condition="旧版本", decision="放历史版本/"),
            ],
            status="approved",
        )
    )
    store.add_skill_version(
        SkillVersion(
            skill_id=skill.id,
            version=2,
            trigger_conditions=["整理课程资料时"],
            decision_rules=[
                DecisionRule.create(rule_id="r1", condition="归档时", decision="放考试资料/"),
                DecisionRule.create(rule_id="r2", condition="旧版本", decision="放历史版本/"),
                DecisionRule.create(
                    rule_id="r3",
                    condition="遇到课程录音",
                    decision="录音与转写稿配对放入 录音/",
                ),
            ],
            status="candidate",
        )
    )
    return skill.id


CASES = [
    {
        "situation": "期末复习提纲需要归档",
        "expected_decision": "放入考试资料/文件夹",
        "keywords": ["考试资料"],
    },
    {
        "situation": "讲座录音和对应的转写稿",
        "expected_decision": "配对放入 录音/ 文件夹",
        "keywords": ["录音", "转写稿"],
    },
]


def test_heuristic_evaluation(store):
    skill_id = _setup_two_versions(store)
    evaluator = SkillEvaluator(store)  # no LLM needed for heuristic
    report = evaluator.evaluate(skill_id, 1, 2, CASES, method="heuristic")

    assert report.method == "heuristic"
    assert report.score_a == 0.5  # v1 passes case 1, fails case 2
    assert report.score_b == 1.0  # v2 passes both
    assert report.results[0]["supported_a"] is True
    assert report.results[1]["supported_a"] is False
    assert report.results[1]["supported_b"] is True
    assert "NOT semantic understanding" in report.notes
    # report persisted
    stored = store.list_eval_reports(skill_id)
    assert len(stored) == 1
    assert stored[0].score_b == 1.0


def test_llm_judge_evaluation(store):
    skill_id = _setup_two_versions(store)
    judge_responses = [
        # case 0 / version a: supported
        json.dumps(
            {"predicted_decision": "考试资料/", "matches_expected": True, "reasoning": "ok"}
        ),
        # case 0 / version b: supported
        json.dumps(
            {"predicted_decision": "考试资料/", "matches_expected": True, "reasoning": "ok"}
        ),
        # case 1 / version a: NOT supported
        json.dumps(
            {"predicted_decision": "参考/", "matches_expected": False, "reasoning": "no rule"}
        ),
        # case 1 / version b: supported
        json.dumps(
            {"predicted_decision": "录音/", "matches_expected": True, "reasoning": "rule r3"}
        ),
    ]
    evaluator = SkillEvaluator(store, ScriptedLLM(judge_responses))
    report = evaluator.evaluate(skill_id, 1, 2, CASES, method="llm_judge")

    assert report.method == "llm_judge"
    assert report.score_a == 0.5
    assert report.score_b == 1.0
    assert "predicted: 参考/" in report.results[1]["detail_a"]
    assert "Judge bias is possible" in report.notes


def test_auto_method_prefers_llm_judge_when_llm_present(store):
    skill_id = _setup_two_versions(store)
    responses = [
        json.dumps({"predicted_decision": "x", "matches_expected": True, "reasoning": "r"}),
        json.dumps({"predicted_decision": "x", "matches_expected": True, "reasoning": "r"}),
        json.dumps({"predicted_decision": "x", "matches_expected": True, "reasoning": "r"}),
        json.dumps({"predicted_decision": "x", "matches_expected": True, "reasoning": "r"}),
    ]
    evaluator = SkillEvaluator(store, ScriptedLLM(responses))
    report = evaluator.evaluate(skill_id, 1, 2, CASES, method="auto")
    assert report.method == "llm_judge"


def test_auto_method_falls_back_to_heuristic(store):
    skill_id = _setup_two_versions(store)
    evaluator = SkillEvaluator(store)
    report = evaluator.evaluate(skill_id, 1, 2, CASES, method="auto")
    assert report.method == "heuristic"


def test_llm_judge_without_llm_raises(store):
    skill_id = _setup_two_versions(store)
    evaluator = SkillEvaluator(store)
    with pytest.raises(LLMNotConfiguredError):
        evaluator.evaluate(skill_id, 1, 2, CASES, method="llm_judge")


def test_evaluate_requires_cases(store):
    skill_id = _setup_two_versions(store)
    evaluator = SkillEvaluator(store)
    with pytest.raises(SchemaValidationError):
        evaluator.evaluate(skill_id, 1, 2, [], method="heuristic")


def test_evaluate_rejects_unknown_method(store):
    skill_id = _setup_two_versions(store)
    evaluator = SkillEvaluator(store)
    with pytest.raises(SchemaValidationError):
        evaluator.evaluate(skill_id, 1, 2, CASES, method="vibes")


def test_malformed_case_rejected(store):
    skill_id = _setup_two_versions(store)
    evaluator = SkillEvaluator(store)
    with pytest.raises(SchemaValidationError):
        evaluator.evaluate(skill_id, 1, 2, [{"situation": "only situation"}], method="heuristic")


def test_heuristic_without_keywords_uses_expected_decision(store):
    skill_id = _setup_two_versions(store)
    cases = [
        {
            "situation": "期末提纲",
            "expected_decision": "考试资料",
        }
    ]
    evaluator = SkillEvaluator(store)
    report = evaluator.evaluate(skill_id, 1, 2, cases, method="heuristic")
    # "考试资料" appears in both versions' rule text
    assert report.score_a == 1.0
    assert report.score_b == 1.0


@pytest.mark.parametrize("verdict", ["false", "true", 0, 1, None, [], {}])
def test_judge_requires_actual_boolean_and_does_not_save_invalid_report(store, verdict):
    skill_id = _setup_two_versions(store)
    response = json.dumps(
        {"predicted_decision": "参考/", "matches_expected": verdict, "reasoning": "no rule"}
    )
    evaluator = SkillEvaluator(store, ScriptedLLM([response]))
    with pytest.raises(SchemaValidationError, match="JSON boolean"):
        evaluator.evaluate(skill_id, 1, 2, CASES, method="llm_judge")
    assert store.list_eval_reports(skill_id) == []


@pytest.mark.parametrize("field", ["predicted_decision", "reasoning"])
@pytest.mark.parametrize("value", [None, "  ", 42])
def test_judge_requires_prediction_and_reasoning(store, field, value):
    skill_id = _setup_two_versions(store)
    response = {"predicted_decision": "参考/", "matches_expected": False, "reasoning": "no rule"}
    response[field] = value
    evaluator = SkillEvaluator(store, ScriptedLLM([json.dumps(response)]))
    with pytest.raises(SchemaValidationError, match=field):
        evaluator.evaluate(skill_id, 1, 2, CASES, method="llm_judge")
    assert store.list_eval_reports(skill_id) == []


@pytest.mark.parametrize(
    "bad_case",
    [
        None,
        {"situation": None, "expected_decision": "x"},
        {"situation": "x", "expected_decision": 5},
        {"situation": "x", "expected_decision": "y", "keywords": "word"},
        {"situation": "x", "expected_decision": "y", "keywords": [""]},
    ],
)
def test_all_cases_validated_before_any_llm_call(store, bad_case):
    skill_id = _setup_two_versions(store)
    llm = ScriptedLLM([])
    with pytest.raises(SchemaValidationError):
        SkillEvaluator(store, llm).evaluate(skill_id, 1, 2, [CASES[0], bad_case])
    assert not llm.calls
    assert store.list_eval_reports(skill_id) == []


def test_user_notes_cannot_replace_method_limitations(store):
    skill_id = _setup_two_versions(store)
    report = SkillEvaluator(store).evaluate(
        skill_id, 1, 2, CASES, method="heuristic", notes="Release review"
    )
    saved = store.list_eval_reports(skill_id)[0]
    assert "NOT semantic understanding" in report.notes
    assert "Release review" in saved.notes
    assert saved.notes == report.notes
