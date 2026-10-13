"""Learning engine tests — pipeline logic with ScriptedLLM only."""

from __future__ import annotations

import json

import pytest

from teachonce.errors import (
    LLMNotConfiguredError,
    SchemaValidationError,
    SensitiveContentError,
)
from teachonce.learning import LearningEngine
from teachonce.llm import ScriptedLLM


def _import_one(store, goal: str = "整理资料") -> str:
    engine = LearningEngine(store)  # import needs no LLM
    demo = engine.import_demonstration(
        {
            "task_goal": goal,
            "steps": [{"action": "把文件按课程分类并归档"}],
            "outcome": "完成",
        }
    )
    return demo.id


def _extraction_payload(sources: list[str]) -> str:
    return json.dumps(
        {
            "summary": "整理课程资料的工作",
            "trigger_conditions": ["需要整理课程相关文件时"],
            "decision_rules": [
                {
                    "condition": "建立目录结构时",
                    "decision": "按课程名称分层，不按文件类型",
                    "rationale": "用户按课程查找资料",
                    "confidence": 0.9,
                    "sources": sources,
                }
            ],
            "exception_handling": [{"situation": "无法判断归属", "handling": "放入 _待整理/"}],
            "anti_patterns": ["按文件扩展名分类"],
            "open_questions": ["未覆盖：手机端截图如何处理"],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_import_requires_goal(store):
    engine = LearningEngine(store)
    with pytest.raises(SchemaValidationError):
        engine.import_demonstration({"task_goal": "  "})


def test_import_requires_steps_or_judgments(store):
    engine = LearningEngine(store)
    with pytest.raises(SchemaValidationError):
        engine.import_demonstration({"task_goal": "只有目标"})


def test_import_rejects_secrets(store):
    engine = LearningEngine(store)
    with pytest.raises(SensitiveContentError):
        engine.import_demonstration(
            {
                "task_goal": "登录后下载资料",
                "steps": [{"action": "输入密码", "decision": "password: hunter2"}],
            }
        )
    assert store.list_demonstrations() == []  # nothing persisted


def test_import_stores_demo(store):
    demo_id = _import_one(store, "自定义目标")
    assert store.get_demonstration(demo_id).task_goal == "自定义目标"


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def test_extract_requires_llm(store):
    demo_id = _import_one(store)
    engine = LearningEngine(store)  # no llm
    with pytest.raises(LLMNotConfiguredError) as exc:
        engine.extract_candidate_skill("name", [demo_id])
    assert "TEACHCONE_LLM_API_KEY" in str(exc.value)


def test_extract_requires_demo_ids(store):
    engine = LearningEngine(store, ScriptedLLM([]))
    with pytest.raises(SchemaValidationError):
        engine.extract_candidate_skill("name", [])


def test_extract_requires_name(store):
    demo_id = _import_one(store)
    engine = LearningEngine(store, ScriptedLLM([]))
    with pytest.raises(SchemaValidationError):
        engine.extract_candidate_skill("   ", [demo_id])


def test_extract_happy_path(store):
    d1 = _import_one(store, "任务一")
    d2 = _import_one(store, "任务二")
    llm = ScriptedLLM([_extraction_payload([d1, d2])])
    engine = LearningEngine(store, llm)

    skill, sv = engine.extract_candidate_skill("课程资料整理", [d1, d2])

    assert sv.version == 1
    assert sv.status == "candidate"
    assert skill.active_version is None
    assert len(sv.decision_rules) == 1
    assert sv.decision_rules[0].sources == [d1, d2]
    assert sv.derived_from == {"demonstrations": [d1, d2]}
    assert sv.exception_handling[0]["handling"] == "放入 _待整理/"
    assert llm.calls[0]["json_mode"] is True
    # round-trips through the store
    assert store.get_skill_version(skill.id, 1).summary == "整理课程资料的工作"


def test_extract_filters_hallucinated_sources(store):
    d1 = _import_one(store)
    llm = ScriptedLLM([_extraction_payload([d1, "demo_does_not_exist"])])
    engine = LearningEngine(store, llm)
    _, sv = engine.extract_candidate_skill("n", [d1])
    rule = sv.decision_rules[0]
    assert rule.sources == [d1]  # hallucinated id dropped
    assert any("demo_does_not_exist" in q for q in sv.open_questions)


def test_extract_rejects_unparseable_response(store):
    d1 = _import_one(store)
    engine = LearningEngine(store, ScriptedLLM(["this is not json"]))
    with pytest.raises(SchemaValidationError):
        engine.extract_candidate_skill("n", [d1])


def test_extract_rejects_response_without_rules(store):
    d1 = _import_one(store)
    empty = json.dumps({"summary": "s", "trigger_conditions": ["c"], "decision_rules": []})
    engine = LearningEngine(store, ScriptedLLM([empty]))
    with pytest.raises(SchemaValidationError):
        engine.extract_candidate_skill("n", [d1])


def test_extract_drops_malformed_rules_but_keeps_valid_ones(store):
    d1 = _import_one(store)
    payload = json.dumps(
        {
            "summary": "s",
            "trigger_conditions": ["c"],
            "decision_rules": [
                {"condition": "", "decision": "d"},  # malformed -> dropped
                {"condition": "c2", "decision": "d2", "sources": [d1]},
            ],
        }
    )
    engine = LearningEngine(store, ScriptedLLM([payload]))
    _, sv = engine.extract_candidate_skill("n", [d1])
    assert len(sv.decision_rules) == 1
    assert sv.decision_rules[0].condition == "c2"
