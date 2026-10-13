"""Model round-trip and tolerance tests."""

from __future__ import annotations

from teachonce.models import (
    Correction,
    DecisionRule,
    Demonstration,
    EvalReport,
    MatchResult,
    Observation,
    Skill,
    SkillVersion,
)


def test_demonstration_roundtrip():
    demo = Demonstration.create(
        task_goal="整理资料",
        steps=[{"action": "建文件夹", "decision": "按课程分", "rationale": "按课找"}],
        context="期末",
        key_judgments=[{"situation": "s", "decision": "d", "why": "w"}],
        outcome="完成",
        tags=["t1"],
    )
    restored = Demonstration.from_dict(demo.to_dict())
    assert restored.id == demo.id
    assert restored.steps[0].action == "建文件夹"
    assert restored.steps[0].rationale == "按课找"
    assert restored.key_judgments == demo.key_judgments
    assert restored.tags == ["t1"]


def test_observation_tolerates_missing_fields():
    obs = Observation.from_dict({"action": "a"})
    assert obs.target == ""
    assert obs.decision == ""


def test_skill_version_roundtrip_with_rules_and_changes():
    sv = SkillVersion(
        skill_id="skill_x",
        version=2,
        trigger_conditions=["cond"],
        decision_rules=[
            DecisionRule.create(
                condition="c",
                decision="d",
                rationale="r",
                confidence=0.8,
                sources=["demo_1"],
            )
        ],
        exception_handling=[{"situation": "s", "handling": "h"}],
        anti_patterns=["ap"],
        changelog="ch",
        changes=[{"op": "add", "rule_id": "rule_1", "description": "new rule"}],
        status="approved",
        derived_from={"demonstrations": ["demo_1"], "corrections": ["corr_1"]},
        open_questions=["q"],
        summary="sum",
    )
    restored = SkillVersion.from_dict(sv.to_dict())
    assert restored.version == 2
    assert restored.status == "approved"
    assert restored.decision_rules[0].condition == "c"
    assert restored.decision_rules[0].sources == ["demo_1"]
    assert restored.changes[0]["op"] == "add"
    assert restored.derived_from["corrections"] == ["corr_1"]


def test_decision_rule_confidence_clamped():
    rule = DecisionRule.create(condition="c", decision="d", confidence=1.7)
    assert rule.confidence == 1.0
    rule2 = DecisionRule.create(condition="c", decision="d", confidence=-3)
    assert rule2.confidence == 0.0


def test_invalid_status_falls_back_to_candidate():
    sv = SkillVersion.from_dict({"skill_id": "s", "version": 1, "status": "bogus"})
    assert sv.status == "candidate"


def test_skill_create_assigns_id():
    skill = Skill.create("name", "desc")
    assert skill.id.startswith("skill_")
    assert skill.active_version is None


def test_correction_roundtrip():
    c = Correction.create(
        skill_id="skill_1",
        target_version=1,
        feedback="录音要单独放",
        correction_type="missing_rule",
        analysis="缺少录音规则",
    )
    restored = Correction.from_dict(c.to_dict())
    assert restored.correction_type == "missing_rule"
    assert restored.status == "open"


def test_correction_invalid_type_becomes_other():
    c = Correction.from_dict(
        {"skill_id": "s", "target_version": 1, "feedback": "f", "correction_type": "wat"}
    )
    assert c.correction_type == "other"


def test_match_result_to_dict():
    m = MatchResult(
        task="t", matched=True, skill_id="s", confidence=0.9, requires_confirmation=False
    )
    d = m.to_dict()
    assert d["matched"] is True
    assert d["confidence"] == 0.9


def test_eval_report_create_and_dict():
    r = EvalReport.create(
        skill_id="s",
        version_a=1,
        version_b=2,
        method="heuristic",
        score_a=0.5,
        score_b=1.0,
        results=[{"case_index": 0}],
        notes="n",
    )
    d = r.to_dict()
    assert d["id"].startswith("eval_")
    assert d["score_b"] == 1.0
