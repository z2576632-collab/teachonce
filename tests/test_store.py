"""Store tests: append-only versions, approval pointers, corrections, logs."""

from __future__ import annotations

import pytest

from teachonce.errors import NotFoundError
from teachonce.models import (
    Correction,
    Demonstration,
    EvalReport,
    MatchResult,
    Skill,
    SkillVersion,
)


def _version(skill_id: str, version: int, *, status: str = "candidate") -> SkillVersion:
    return SkillVersion(
        skill_id=skill_id,
        version=version,
        trigger_conditions=[f"cond v{version}"],
        status=status,  # type: ignore[arg-type]
    )


def test_add_and_get_demonstration(store):
    demo = Demonstration.create("goal", steps=[{"action": "a"}])
    store.add_demonstration(demo)
    fetched = store.get_demonstration(demo.id)
    assert fetched.task_goal == "goal"
    assert len(store.list_demonstrations()) == 1


def test_demonstration_not_found(store):
    with pytest.raises(NotFoundError):
        store.get_demonstration("demo_nope")


def test_skill_version_flow(store):
    skill = Skill.create("n", "d")
    store.create_skill(skill)
    assert store.get_active_version(skill.id) is None  # nothing approved yet

    store.add_skill_version(_version(skill.id, 1))
    store.add_skill_version(_version(skill.id, 2))
    versions = store.list_skill_versions(skill.id)
    assert [v.version for v in versions] == [1, 2]
    assert store.next_version_number(skill.id) == 3

    store.set_version_status(skill.id, 2, "approved")
    store.set_active_version(skill.id, 2)
    active = store.get_active_version(skill.id)
    assert active is not None
    assert active.version == 2
    assert active.status == "approved"


def test_duplicate_version_rejected(store):
    skill = Skill.create("n")
    store.create_skill(skill)
    store.add_skill_version(_version(skill.id, 1))
    with pytest.raises(ValueError) as exc:
        store.add_skill_version(_version(skill.id, 1))
    assert "append-only" in str(exc.value)


def test_set_status_missing_version_raises(store):
    skill = Skill.create("n")
    store.create_skill(skill)
    with pytest.raises(NotFoundError):
        store.set_version_status(skill.id, 9, "approved")


def test_corrections_flow(store):
    skill = Skill.create("n")
    store.create_skill(skill)
    c1 = Correction.create(skill.id, 1, "fix one")
    c2 = Correction.create(skill.id, 1, "fix two")
    store.add_correction(c1)
    store.add_correction(c2)
    assert len(store.list_corrections(skill.id, status="open")) == 2

    store.mark_correction_applied(c1.id, 2)
    open_now = store.list_corrections(skill.id, status="open")
    assert [c.id for c in open_now] == [c2.id]
    applied = store.get_correction(c1.id)
    assert applied.status == "applied"
    assert applied.applied_in_version == 2


def test_eval_report_roundtrip(store):
    skill = Skill.create("n")
    store.create_skill(skill)
    report = EvalReport.create(
        skill.id,
        1,
        2,
        "heuristic",
        0.5,
        1.0,
        results=[{"case_index": 0, "supported_a": False, "supported_b": True}],
        notes="notes",
    )
    store.save_eval_report(report)
    reports = store.list_eval_reports(skill.id)
    assert len(reports) == 1
    assert reports[0].results[0]["supported_b"] is True
    assert reports[0].method == "heuristic"


def test_match_log(store):
    result = MatchResult(task="t", matched=False, requires_confirmation=True)
    store.log_match(result)
    logs = store.list_match_logs()
    assert len(logs) == 1
    assert logs[0]["task"] == "t"
    assert logs[0]["requires_confirmation"] is True


def test_update_skill_meta(store):
    skill = Skill.create("old name", "old desc")
    store.create_skill(skill)
    store.update_skill_meta(skill.id, description="new desc")
    assert store.get_skill(skill.id).description == "new desc"
