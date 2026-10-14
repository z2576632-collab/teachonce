"""Matching engine tests — confidence thresholds, safety gates, no LLM leaks."""

from __future__ import annotations

import json

import pytest

from teachonce.errors import SchemaValidationError
from teachonce.llm import ScriptedLLM
from teachonce.matching import SkillMatcher
from teachonce.models import DecisionRule, Skill, SkillVersion


def _setup_approved_skill(store) -> str:
    skill = Skill.create("课程资料整理", "整理课程资料")
    store.create_skill(skill)
    sv = SkillVersion(
        skill_id=skill.id,
        version=1,
        trigger_conditions=["需要整理课程相关文件时"],
        decision_rules=[
            DecisionRule.create(condition="建立目录时", decision="按课程分", rationale="按课找")
        ],
        summary="整理课程资料的工作",
        status="approved",
    )
    store.add_skill_version(sv)
    store.set_active_version(skill.id, 1)
    return skill.id


def _match_payload(skill_id: str | None, confidence: float) -> str:
    return json.dumps(
        {
            "matched_skill_id": skill_id,
            "confidence": confidence,
            "rationale": "任务主题与技能触发条件一致",
            "plan": ["按课程建立文件夹", "把资料放入对应子目录"],
            "missing_info": [],
            "risk_notes": "",
        },
        ensure_ascii=False,
    )


def test_no_skills_short_circuits_without_llm(store):
    matcher = SkillMatcher(store)  # no LLM at all
    result = matcher.match("整理课程资料")
    assert result.matched is False
    assert result.requires_confirmation is True
    assert "No approved skills" in result.rationale
    assert len(store.list_match_logs()) == 1


def test_unapproved_versions_are_not_candidates(store):
    skill = Skill.create("草稿技能")
    store.create_skill(skill)
    store.add_skill_version(
        SkillVersion(skill_id=skill.id, version=1, trigger_conditions=["x"])
    )  # candidate, never approved
    matcher = SkillMatcher(store)
    result = matcher.match("任何任务")
    assert result.matched is False


def test_match_happy_path(store):
    skill_id = _setup_approved_skill(store)
    llm = ScriptedLLM([_match_payload(skill_id, 0.9)])
    matcher = SkillMatcher(store, llm)
    result = matcher.match("整理《学术写作》的课程资料")
    assert result.matched is True
    assert result.skill_id == skill_id
    assert result.version == 1
    assert result.confidence == 0.9
    assert result.requires_confirmation is False
    assert len(result.plan) == 2
    assert llm.calls[0]["json_mode"] is True


def test_low_confidence_requires_confirmation(store):
    skill_id = _setup_approved_skill(store)
    matcher = SkillMatcher(store, ScriptedLLM([_match_payload(skill_id, 0.4)]))
    result = matcher.match("随便看看")
    assert result.matched is True
    assert result.requires_confirmation is True  # below 0.6 threshold


def test_missing_info_requires_confirmation(store):
    skill_id = _setup_approved_skill(store)
    payload = json.dumps(
        {
            "matched_skill_id": skill_id,
            "confidence": 0.95,
            "rationale": "r",
            "plan": [],
            "missing_info": ["需要知道课程名称"],
            "risk_notes": "",
        },
        ensure_ascii=False,
    )
    matcher = SkillMatcher(store, ScriptedLLM([payload]))
    result = matcher.match("整理资料")
    assert result.requires_confirmation is True
    assert result.missing_info == ["需要知道课程名称"]


def test_hallucinated_skill_id_is_no_match(store):
    _setup_approved_skill(store)
    matcher = SkillMatcher(store, ScriptedLLM([_match_payload("skill_fake", 0.99)]))
    result = matcher.match("整理资料")
    assert result.matched is False
    assert result.requires_confirmation is True
    assert "unknown skill id" in result.rationale


def test_dangerous_task_forces_confirmation(store):
    skill_id = _setup_approved_skill(store)
    matcher = SkillMatcher(store, ScriptedLLM([_match_payload(skill_id, 0.95)]))
    result = matcher.match("删除所有旧课件并清空历史版本")
    assert result.matched is True
    assert result.requires_confirmation is True
    assert "destructive" in result.rationale


def test_null_match_response(store):
    _setup_approved_skill(store)
    matcher = SkillMatcher(store, ScriptedLLM([_match_payload(None, 0.1)]))
    result = matcher.match("写代码")
    assert result.matched is False
    assert result.requires_confirmation is True


def test_empty_task_rejected(store):
    matcher = SkillMatcher(store)
    with pytest.raises(SchemaValidationError):
        matcher.match("   ")


def test_skill_ids_filter(store):
    skill_id = _setup_approved_skill(store)
    _setup_approved_skill(store)
    matcher = SkillMatcher(store, ScriptedLLM([_match_payload(skill_id, 0.9)]))
    result = matcher.match("整理资料", skill_ids=[skill_id])
    assert result.matched is True
    # only one candidate was offered to the LLM
    assert matcher.llm.calls[0]["user"].count("## skill id=") == 1
