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
