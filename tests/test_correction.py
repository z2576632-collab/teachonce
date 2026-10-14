"""Correction engine tests — version flow, changelog, approval, rollback."""

from __future__ import annotations

import json

import pytest

from teachonce.correction import CorrectionEngine
from teachonce.errors import (
    NotFoundError,
    SchemaValidationError,
    SensitiveContentError,
    TeachOnceError,
    VersionStateError,
)
from teachonce.llm import ScriptedLLM
from teachonce.models import Correction, DecisionRule, Skill, SkillVersion


def _setup_v1(store) -> tuple[str, SkillVersion]:
    skill = Skill.create("课程资料整理", "desc")
    store.create_skill(skill)
    sv = SkillVersion(
        skill_id=skill.id,
        version=1,
        trigger_conditions=["整理课程文件时"],
        decision_rules=[
            DecisionRule.create(
                rule_id="rule_base",
                condition="归档资料时",
                decision="按课程分文件夹",
                rationale="按课查找",
                sources=["demo_1"],
            )
        ],
        status="approved",
        derived_from={"demonstrations": ["demo_1"]},
        changelog="initial",
    )
    store.add_skill_version(sv)
    store.set_active_version(skill.id, 1)
    return skill.id, sv


def _record_payload() -> str:
    return json.dumps(
        {"correction_type": "missing_rule", "analysis": "缺少录音处理规则"},
        ensure_ascii=False,
    )


def _revision_payload(correction_id: str) -> str:
    return json.dumps(
        {
            "summary": "整理课程资料（含录音规则）",
            "trigger_conditions": ["整理课程文件时"],
            "decision_rules": [
                {
                    "id": "rule_base",
                    "condition": "归档资料时",
                    "decision": "按课程分文件夹",
                    "rationale": "按课查找",
                    "confidence": 0.8,
                    "sources": ["demo_1"],
                },
                {
                    "id": "rule_new_1",
                    "condition": "遇到课程录音时",
                    "decision": "放入该课程 录音/ 文件夹",
                    "rationale": "用户要求录音有自己的位置",
                    "confidence": 0.9,
                    "sources": [correction_id],
                },
            ],
            "exception_handling": [],
            "anti_patterns": [],
            "open_questions": [],
            "changelog": "新增录音处理规则：录音集中放入课程 录音/ 文件夹。",
            "changes": [
                {
                    "op": "add",
                    "rule_id": "rule_new_1",
                    "description": "新增录音处理规则",
                }
            ],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def test_record_correction_classifies(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store, ScriptedLLM([_record_payload()]))
    correction = engine.record_correction(skill_id, "录音不应该放进参考文件夹")
    assert correction.correction_type == "missing_rule"
    assert correction.target_version == 1
    assert correction.status == "open"
    assert correction.analysis == "缺少录音处理规则"


def test_record_rejects_secrets(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store, ScriptedLLM([_record_payload()]))
    with pytest.raises(SensitiveContentError):
        engine.record_correction(skill_id, "token: abcdef1234567890abcdef")
    assert store.list_corrections(skill_id) == []


def test_record_rejects_empty_feedback(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store, ScriptedLLM([]))
    with pytest.raises(SchemaValidationError):
        engine.record_correction(skill_id, "   ")


def test_record_unknown_correction_type_becomes_other(store):
    skill_id, _ = _setup_v1(store)
    payload = json.dumps({"correction_type": "nonsense", "analysis": "a"})
    engine = CorrectionEngine(store, ScriptedLLM([payload]))
    correction = engine.record_correction(skill_id, "随便说点什么")
    assert correction.correction_type == "other"


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------


def test_apply_requires_open_corrections(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store, ScriptedLLM([]))
    with pytest.raises(TeachOnceError) as exc:
        engine.apply_correction(skill_id)
    assert "no open corrections" in str(exc.value).lower()


def test_apply_creates_new_version_with_changes(store):
    skill_id, _ = _setup_v1(store)
    record_llm = ScriptedLLM([_record_payload()])
    engine = CorrectionEngine(store, record_llm)
    correction = engine.record_correction(skill_id, "录音要单独放 录音/ 文件夹")

    apply_llm = ScriptedLLM([_revision_payload(correction.id)])
    engine2 = CorrectionEngine(store, apply_llm)
    v2 = engine2.apply_correction(skill_id)

    assert v2.version == 2
    assert v2.status == "candidate"
    assert len(v2.decision_rules) == 2
    assert v2.changes[0]["op"] == "add"
    assert v2.derived_from["corrections"] == [correction.id]
    assert v2.derived_from["demonstrations"] == ["demo_1"]  # inherited
    # correction marked applied, v1 untouched
    assert store.get_correction(correction.id).status == "applied"
    assert store.get_skill_version(skill_id, 1).changelog == "initial"
    # still candidate: active pointer must not move
    assert store.get_skill(skill_id).active_version == 1


def test_apply_with_explicit_correction_ids(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store, ScriptedLLM([_record_payload()]))
    c1 = engine.record_correction(skill_id, "反馈一")

    apply_engine = CorrectionEngine(store, ScriptedLLM([_revision_payload(c1.id)]))
    v2 = apply_engine.apply_correction(skill_id, correction_ids=[c1.id])
    assert v2.version == 2
    assert store.get_correction(c1.id).status == "applied"


# ---------------------------------------------------------------------------
# approve / rollback
# ---------------------------------------------------------------------------


def test_approve_and_rollback_flow(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store, ScriptedLLM([_record_payload()]))
    correction = engine.record_correction(skill_id, "录音规则")
    engine2 = CorrectionEngine(store, ScriptedLLM([_revision_payload(correction.id)]))
    engine2.apply_correction(skill_id)

    # approve v2
    skill = engine2.approve_version(skill_id, 2)
    assert skill.active_version == 2
    assert store.get_skill_version(skill_id, 2).status == "approved"

    # rollback to v1
    skill = engine2.rollback_to(skill_id, 1)
    assert skill.active_version == 1
    assert store.get_skill_version(skill_id, 2).status == "rolled_back"
    assert store.get_skill_version(skill_id, 1).status == "approved"


def test_rollback_to_candidate_rejected(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store, ScriptedLLM([_record_payload()]))
    correction = engine.record_correction(skill_id, "x")
    engine2 = CorrectionEngine(store, ScriptedLLM([_revision_payload(correction.id)]))
    engine2.apply_correction(skill_id)  # v2 is a candidate

    with pytest.raises(VersionStateError):
        engine2.rollback_to(skill_id, 2)


def test_rollback_to_active_is_noop(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store)
    skill = engine.rollback_to(skill_id, 1)  # v1 already active + approved
    assert skill.active_version == 1


def test_approve_requires_existing_version(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store)
    with pytest.raises(NotFoundError):
        engine.approve_version(skill_id, 99)


def test_second_revision_preserves_prior_evidence_and_rule_ids(store):
    skill_id, _ = _setup_v1(store)
    engine = CorrectionEngine(store, ScriptedLLM([_record_payload()]))
    c1 = engine.record_correction(skill_id, "录音单独存放")
    engine.llm = ScriptedLLM([_revision_payload(c1.id)])
    v2 = engine.apply_correction(skill_id)
    engine.approve_version(skill_id, v2.version)
    before = [v.to_dict() for v in store.list_skill_versions(skill_id)]

    engine.llm = ScriptedLLM([_record_payload()])
    c2 = engine.record_correction(skill_id, "课程目录使用完整课程名")
    payload = json.loads(_revision_payload(c1.id))
    payload["decision_rules"][0]["decision"] = "按完整课程名分文件夹"
    payload["decision_rules"][0]["sources"].append(c2.id)
    payload["changes"] = [{"op": "modify", "rule_id": "rule_base", "description": "使用完整课程名"}]
    engine.llm = ScriptedLLM([json.dumps(payload)])
    v3 = engine.apply_correction(skill_id)

    assert v3.version == 3 and v3.status == "candidate"
    assert v3.derived_from["corrections"] == [c1.id, c2.id]
    assert v3.decision_rules[1].sources == [c1.id]
    assert [r.id for r in v3.decision_rules] == [r.id for r in v2.decision_rules]
    assert v3.changes[0]["rule_id"] == v3.decision_rules[0].id
    assert not any(c1.id in question for question in v3.open_questions)
    assert [v.to_dict() for v in store.list_skill_versions(skill_id)[:2]] == before
    assert store.get_skill(skill_id).active_version == 2


def test_correction_from_another_skill_is_rejected_before_llm(store):
    skill_id, _ = _setup_v1(store)
    other_id, _ = _setup_v1(store)
    correction = Correction.create(other_id, 1, "Feedback for another skill")
    store.add_correction(correction)
    llm = ScriptedLLM([])
    with pytest.raises(SchemaValidationError, match="another skill"):
        CorrectionEngine(store, llm).apply_correction(skill_id, correction_ids=[correction.id])
    assert not llm.calls
    assert store.get_correction(correction.id).status == "open"
    assert len(store.list_skill_versions(skill_id)) == 1


def test_applied_correction_cannot_be_reused(store):
    skill_id, _ = _setup_v1(store)
    correction = Correction.create(skill_id, 1, "New recording rule")
    store.add_correction(correction)
    engine = CorrectionEngine(store, ScriptedLLM([_revision_payload(correction.id)]))
    v2 = engine.apply_correction(skill_id)
    llm = ScriptedLLM([])
    with pytest.raises(VersionStateError, match="already applied"):
        CorrectionEngine(store, llm).apply_correction(skill_id, correction_ids=[correction.id])
    assert not llm.calls
    assert store.get_correction(correction.id).applied_in_version == v2.version
    assert len(store.list_skill_versions(skill_id)) == 2


def test_duplicate_correction_ids_rejected_before_llm(store):
    skill_id, _ = _setup_v1(store)
    correction = Correction.create(skill_id, 1, "New rule")
    store.add_correction(correction)
    llm = ScriptedLLM([])
    with pytest.raises(SchemaValidationError, match="unique"):
        CorrectionEngine(store, llm).apply_correction(
            skill_id, correction_ids=[correction.id, correction.id]
        )
    assert not llm.calls
    assert store.get_correction(correction.id).status == "open"


def test_correction_consumed_during_llm_call_does_not_create_extra_version(store):
    skill_id, _ = _setup_v1(store)
    correction = Correction.create(skill_id, 1, "New rule")
    store.add_correction(correction)

    def competing_revision(system, user):
        other = CorrectionEngine(store, ScriptedLLM([_revision_payload(correction.id)]))
        other.apply_correction(skill_id)
        return _revision_payload(correction.id)

    engine = CorrectionEngine(store, ScriptedLLM([competing_revision]))
    with pytest.raises(VersionStateError, match="no longer open"):
        engine.apply_correction(skill_id)
    assert [v.version for v in store.list_skill_versions(skill_id)] == [1, 2]
    assert store.get_correction(correction.id).applied_in_version == 2
    assert store.get_skill(skill_id).active_version == 1
