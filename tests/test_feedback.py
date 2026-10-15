"""Reported execution feedback is durable, atomic and never auto-approved."""

from __future__ import annotations

import sqlite3

import pytest

from teachonce.errors import NotFoundError, SchemaValidationError, SensitiveContentError
from teachonce.feedback import FeedbackEngine
from teachonce.models import Skill, SkillVersion
from teachonce.store import SkillStore


def skill_with_version(store):
    skill = Skill.create("Courses")
    store.create_skill_with_version(skill, SkillVersion(skill_id=skill.id, version=1))
    return skill


def test_outcome_and_feedback_resume_after_restart_without_rule_changes(store):
    skill = skill_with_version(store)
    before = store.get_skill_version(skill.id, 1).to_dict()
    outcome = FeedbackEngine(store).record_outcome(
        skill.id,
        1,
        "Organize notes",
        "partial",
        "Host copied two files; one needs review",
        feedback="Unknown files should remain in review",
    )
    with SkillStore(store.db_path) as reopened:
        assert reopened.list_outcomes(skill.id) == [outcome]
        feedback = reopened.get_correction(outcome["correction_id"])
        assert feedback.status == "open" and feedback.target_version == 1
        assert reopened.get_skill_version(skill.id, 1).to_dict() == before
        assert reopened.get_skill(skill.id).active_version is None
        assert "not independently verified" in outcome["provenance"]


@pytest.mark.parametrize("status", ["succeeded", "failed", "partial", "cancelled"])
def test_outcome_without_explicit_feedback_does_not_invent_rules(store, status):
    skill = skill_with_version(store)
    result = FeedbackEngine(store).record_outcome(skill.id, 1, "Task", status, "Host report")
    assert result["correction_id"] is None
    assert store.list_corrections(skill.id) == []


@pytest.mark.parametrize(
    "status,summary,error",
    [
        ("unknown", "Report", SchemaValidationError),
        ("failed", "", SchemaValidationError),
        ("failed", "password=synthetic", SensitiveContentError),
    ],
)
def test_invalid_or_sensitive_outcomes_leave_no_records(store, status, summary, error):
    skill = skill_with_version(store)
    with pytest.raises(error):
        FeedbackEngine(store).record_outcome(
            skill.id, 1, "Task", status, summary, feedback="Fix it"
        )
    assert not store.list_outcomes(skill.id) and not store.list_corrections(skill.id)


def test_outcome_write_failure_rolls_back_linked_correction(store):
    skill = skill_with_version(store)
    store._conn.execute(
        "CREATE TRIGGER fail_outcome BEFORE INSERT ON outcomes BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        FeedbackEngine(store).record_outcome(
            skill.id, 1, "Task", "failed", "Host failure", feedback="Fix it"
        )
    assert not store.list_corrections(skill.id)
    assert not store.list_outcomes(skill.id)


def test_unknown_outcome_version_is_rejected(store):
    skill = skill_with_version(store)
    with pytest.raises(NotFoundError):
        FeedbackEngine(store).record_outcome(skill.id, 99, "Task", "failed", "Host failure")


def test_feedback_tools_use_real_mcp_boundary(store):
    import asyncio

    from teachonce.mcp_server import build_server

    skill = skill_with_version(store)
    server = build_server(store=store)

    async def run():
        recorded = (
            await server.call_tool(
                "teachonce_record_outcome",
                {
                    "skill_id": skill.id,
                    "version": 1,
                    "task": "Group notes",
                    "status": "failed",
                    "summary": "Host could not identify course",
                    "feedback": "Ask when course is unknown",
                },
            )
        ).structured_content
        saved = (
            await server.call_tool("teachonce_list_outcomes", {"skill_id": skill.id})
        ).structured_content
        assert saved == {"outcomes": [recorded], "count": 1}

    asyncio.run(run())
