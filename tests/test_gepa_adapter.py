"""Tests for the GEPA optimization adapter.

All offline: the optimizer callable is dependency-injected (fake), and the
LLM is ScriptedLLM. The real ``gepa`` package is never imported here.
"""

from __future__ import annotations

import json

import pytest

from teachonce.adapters import gepa_optimizer
from teachonce.adapters.gepa_optimizer import optimize_skill_rules
from teachonce.errors import (
    AdapterUnavailableError,
    LLMNotConfiguredError,
    NotFoundError,
    SchemaValidationError,
    SensitiveContentError,
)
from teachonce.llm import ScriptedLLM
from teachonce.models import DecisionRule, Skill, SkillVersion


def _setup_skill(store) -> str:
    skill = Skill.create("课程资料整理")
    store.create_skill(skill)
    sv = SkillVersion(
        skill_id=skill.id,
        version=1,
        trigger_conditions=["整理课程资料时"],
        decision_rules=[
            DecisionRule.create(
                "整理任何课程资料",
                "按课程名建顶层文件夹",
                rationale="用户按课程找资料",
                sources=["demo_1"],
                rule_id="rule_top",
            )
        ],
        anti_patterns=["按文件类型建顶层目录"],
        summary="按课程归档资料",
        status="approved",
        derived_from={"demonstrations": ["demo_1"], "corrections": []},
    )
    store.add_skill_version(sv)
    store.set_active_version(skill.id, 1)
    return skill.id


CASES = [
    {"situation": "期末周有复习提纲和真题", "expected_decision": "放入 考试资料/"},
    {"situation": "一份选课指南属于多门课", "expected_decision": "放入 _公共/"},
]

_OPTIMIZED_TEXT = (
    "整理课程资料时按课程名建顶层文件夹；考试直接相关资料放入 考试资料/；跨课程文件放入 _公共/"
)

_RESTRUCTURE_RESPONSE = json.dumps(
    {
        "summary": "按课程归档资料",
        "trigger_conditions": ["整理课程资料时"],
        "decision_rules": [
            {
                "id": "rule_top",
                "condition": "整理任何课程资料",
                "decision": "按课程名建顶层文件夹",
                "rationale": "用户按课程找资料",
                "confidence": 0.8,
                "sources": ["demo_1"],
            },
            {
                "id": "rule_new_1",
                "condition": "资料与考试直接相关",
                "decision": "放入 考试资料/",
                "rationale": "考试周集中查找",
                "confidence": 0.7,
                "sources": [],
            },
        ],
        "exception_handling": [],
        "anti_patterns": ["按文件类型建顶层目录"],
        "open_questions": [],
    }
)


def _fake_optimize(**kwargs):
    """Stand-in for gepa.optimize_anything: scores via injected evaluator."""
    assert kwargs["seed_candidate"].strip()
    assert kwargs["objective"]
    assert kwargs["max_metric_calls"] > 0
    # Exercise the evaluator seam like the real engine would.
    score, side = kwargs["evaluator"](_OPTIMIZED_TEXT, kwargs["dataset"][0])
    assert score == 1.0 and side["feedback"]

    class Result:
        best_candidate = _OPTIMIZED_TEXT
        best_score = 1.0

    return Result()


def _judge_then_restructure_llm() -> ScriptedLLM:
    return ScriptedLLM(
        [
            # one judge call from _fake_optimize's evaluator probe
            json.dumps(
                {
                    "matches_expected": True,
                    "predicted_decision": "放入 考试资料/",
                    "reasoning": "规则明确覆盖考试资料",
                }
            ),
            _RESTRUCTURE_RESPONSE,
        ]
    )


def test_optimize_produces_candidate_preserving_base(store):
    skill_id = _setup_skill(store)
    outcome = optimize_skill_rules(
        store,
        _judge_then_restructure_llm(),
        skill_id,
        CASES,
        max_metric_calls=10,
        optimize_fn=_fake_optimize,
    )
    assert outcome.base_version == 1
    assert outcome.best_score == 1.0
    new = outcome.new_version
    assert new.version == 2
    assert new.status == "candidate"
    assert "GEPA" in new.changelog
    assert new.derived_from["optimizations"] == ["gepa:v1:budget10"]
    # Base version untouched, history append-only, active pointer unmoved.
    base = store.get_skill_version(skill_id, 1)
    assert base.status == "approved"
    assert len(store.list_skill_versions(skill_id)) == 2
    assert store.get_skill(skill_id).active_version == 1


def test_optimize_requires_llm(store):
    skill_id = _setup_skill(store)
    with pytest.raises(LLMNotConfiguredError):
        optimize_skill_rules(store, None, skill_id, CASES, optimize_fn=_fake_optimize)


def test_optimize_validates_cases_before_running(store):
    skill_id = _setup_skill(store)
    called = []

    def spy(**kwargs):
        called.append(kwargs)

    with pytest.raises(SchemaValidationError):
        optimize_skill_rules(
            store,
            ScriptedLLM([]),
            skill_id,
            [{"situation": "", "expected_decision": "x"}],
            optimize_fn=spy,
        )
    assert not called


def test_optimize_rejects_empty_optimizer_output(store):
    skill_id = _setup_skill(store)

    def empty_result(**kwargs):
        class Result:
            best_candidate = "   "

        return Result()

    with pytest.raises(SchemaValidationError, match="empty candidate"):
        optimize_skill_rules(store, ScriptedLLM([]), skill_id, CASES, optimize_fn=empty_result)


def test_optimize_unknown_skill_raises(store):
    with pytest.raises(NotFoundError):
        optimize_skill_rules(
            store, ScriptedLLM([]), "skill_missing", CASES, optimize_fn=_fake_optimize
        )


def test_missing_gepa_package_raises_actionable_error(monkeypatch):
    monkeypatch.setattr(gepa_optimizer, "gepa_available", lambda: False)
    with pytest.raises(AdapterUnavailableError, match=r"teachonce\[gepa\]"):
        gepa_optimizer._require_gepa()


def test_optimization_cases_cannot_be_reused_as_held_out_after_restart(store):
    from teachonce.evaluation import SkillEvaluator
    from teachonce.store import SkillStore

    skill_id = _setup_skill(store)
    outcome = optimize_skill_rules(
        store, _judge_then_restructure_llm(), skill_id, CASES, optimize_fn=_fake_optimize
    )
    reopened = SkillStore(store.db_path)
    try:
        llm = ScriptedLLM([])
        with pytest.raises(SchemaValidationError, match="optimization"):
            SkillEvaluator(reopened, llm).evaluate(skill_id, 1, outcome.new_version.version, CASES)
        assert not llm.calls
        assert reopened.list_eval_reports(skill_id) == []
    finally:
        reopened.close()


@pytest.mark.parametrize("field", ["predicted_decision", "reasoning"])
@pytest.mark.parametrize("value", [None, "  ", 42, {}])
def test_optimization_judge_rejects_invalid_text_without_saving(store, field, value):
    skill_id = _setup_skill(store)
    response = {"matches_expected": True, "predicted_decision": "考试资料/", "reasoning": "ok"}
    response[field] = value
    llm = ScriptedLLM([json.dumps(response)])
    with pytest.raises(SchemaValidationError, match=field):
        optimize_skill_rules(store, llm, skill_id, CASES, optimize_fn=_fake_optimize)
    assert len(store.list_skill_versions(skill_id)) == 1


@pytest.mark.parametrize("verdict", ["true", "false", 0, 1, None])
def test_optimization_judge_requires_boolean(store, verdict):
    skill_id = _setup_skill(store)
    llm = ScriptedLLM(
        [
            json.dumps(
                {
                    "matches_expected": verdict,
                    "predicted_decision": "Keep",
                    "reasoning": "ok",
                }
            )
        ]
    )
    with pytest.raises(SchemaValidationError, match="JSON boolean"):
        optimize_skill_rules(store, llm, skill_id, CASES, optimize_fn=_fake_optimize)
    assert len(store.list_skill_versions(skill_id)) == 1


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "10", None])
def test_invalid_budget_rejected_before_optimizer_or_llm(store, budget):
    skill_id = _setup_skill(store)
    llm = ScriptedLLM([])
    with pytest.raises(SchemaValidationError, match="positive integer"):
        optimize_skill_rules(
            store, llm, skill_id, CASES, max_metric_calls=budget, optimize_fn=_fake_optimize
        )
    assert not llm.calls


@pytest.mark.parametrize("sensitive", [False, True])
def test_duplicate_or_sensitive_tuning_cases_rejected_before_optimizer(store, sensitive):
    skill_id = _setup_skill(store)
    cases = [CASES[0], {**CASES[0], "expected_decision": "Different answer"}]
    if sensitive:
        cases[1] = {"situation": "new", "expected_decision": "password=synthetic-value"}
    llm = ScriptedLLM([])
    error = SensitiveContentError if sensitive else SchemaValidationError
    with pytest.raises(error):
        optimize_skill_rules(store, llm, skill_id, cases, optimize_fn=_fake_optimize)
    assert not llm.calls
    assert len(store.list_skill_versions(skill_id)) == 1


def test_multiple_optimizations_and_correction_preserve_case_isolation(store):
    from teachonce.correction import CorrectionEngine
    from teachonce.evaluation import SkillEvaluator
    from teachonce.models import Correction

    skill_id = _setup_skill(store)
    first = optimize_skill_rules(
        store, _judge_then_restructure_llm(), skill_id, CASES, optimize_fn=_fake_optimize
    ).new_version
    new_cases = [{"situation": "A different tuning situation", "expected_decision": "Keep"}]
    second = optimize_skill_rules(
        store,
        _judge_then_restructure_llm(),
        skill_id,
        new_cases,
        base_version=first.version,
        optimize_fn=_fake_optimize,
    ).new_version
    assert len(second.derived_from["optimization_cases"]) == 3
    assert set(first.derived_from["optimization_cases"]) < set(
        second.derived_from["optimization_cases"]
    )
    assert len(second.derived_from["optimizations"]) == 2

    feedback = Correction.create(skill_id, second.version, "Keep the course directory structure")
    store.add_correction(feedback)
    revised_payload = json.loads(_RESTRUCTURE_RESPONSE)
    revised_payload["changelog"] = "Retain course folders"
    revised_payload["changes"] = []
    corrected = CorrectionEngine(
        store, ScriptedLLM([json.dumps(revised_payload)])
    ).apply_correction(
        skill_id,
        base_version=second.version,
    )
    assert corrected.derived_from["optimization_cases"] == second.derived_from["optimization_cases"]
    assert corrected.derived_from["optimizations"] == second.derived_from["optimizations"]
    assert corrected.derived_from["corrections"] == [feedback.id]
    assert store.get_skill(skill_id).active_version == 1

    # Altering answers, whitespace, Unicode width or IDs cannot disguise tuning cases.
    disguised = {
        **new_cases[0],
        "situation": "  Ａ  different\ntuning situation ",
        "expected_decision": "Changed answer",
        "id": "new-id",
    }
    llm = ScriptedLLM([])
    with pytest.raises(SchemaValidationError, match="optimization"):
        SkillEvaluator(store, llm).evaluate(skill_id, 1, corrected.version, [disguised])
    assert not llm.calls
    # Check either compared version, even when the other is the older baseline.
    with pytest.raises(SchemaValidationError, match="optimization"):
        SkillEvaluator(store).evaluate(skill_id, corrected.version, 1, CASES, method="heuristic")
    report = SkillEvaluator(store).evaluate(
        skill_id,
        1,
        corrected.version,
        [
            {
                "situation": "Independent new course exercise",
                "expected_decision": "考试资料/",
                "keywords": ["考试资料"],
            }
        ],
        method="heuristic",
    )
    assert len(store.list_eval_reports(skill_id)) == 1
    assert report.results[0]["supported_b"] is True
