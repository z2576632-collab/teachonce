"""Explicit host proposals use the real lifecycle, not a pretend provider."""

from __future__ import annotations

import asyncio

import pytest

from teachonce.errors import SchemaValidationError
from teachonce.llm import OpenAICompatibleLLM
from teachonce.mcp_server import build_server


def test_host_can_learn_correct_match_and_review_without_api_key(store, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Explicit host proposals must never resolve an API provider")

    monkeypatch.setattr(OpenAICompatibleLLM, "from_env", forbidden)
    mcp = build_server(store=store)

    async def run():
        async def call(tool_name, **args):
            return (await mcp.call_tool(tool_name, args)).structured_content

        demo = await call(
            "teachonce_import_demonstration",
            task_goal="Group notes by course",
            steps=[{"action": "Group course notes", "decision": "Keep course folders"}],
        )
        context = await call(
            "teachonce_learning_context",
            name="Courses",
            demonstration_ids=[demo["demonstration_id"]],
        )
        assert context["server_inference_performed"] is False
        assert "Keep course folders" in context["user_prompt"]
        # This is a synthetic caller proposal testing plumbing, not a model benchmark.
        proposal = {
            "summary": "Course folders",
            "trigger_conditions": ["Course notes"],
            "decision_rules": [
                {
                    "id": "base",
                    "condition": "Course identified",
                    "decision": "Keep course folders",
                    "sources": [demo["demonstration_id"]],
                }
            ],
        }
        generated = await call(
            "teachonce_generate_skill",
            name="Courses",
            demonstration_ids=[demo["demonstration_id"]],
            proposal=proposal,
        )
        assert generated["inference_source"] == "host-proposal"
        skill_id = generated["skill_id"]
        report = await call(
            "teachonce_evaluate_versions",
            skill_id=skill_id,
            version_a=1,
            version_b=1,
            cases=[
                {
                    "situation": "Independent synthetic course",
                    "expected_decision": "Keep course folders",
                }
            ],
            method="heuristic",
        )
        await call(
            "teachonce_approve_version",
            skill_id=skill_id,
            version=1,
            evaluation_id=report["id"],
            confirmed=True,
            reviewer="test human",
            reason="Read rules and smoke report",
        )
        catalog = await call("teachonce_matching_context", task="Organize next semester")
        assert "version: 1" in catalog["user_prompt"]
        matched = await call(
            "teachonce_match_skill",
            task="Organize next semester",
            proposal={
                "matched_skill_id": skill_id,
                "matched_version": 1,
                "confidence": 0.9,
                "rationale": "Course notes match",
                "plan": ["Keep course folders"],
                "missing_info": [],
                "risk_notes": "",
            },
        )
        assert matched["matched"] and matched["version"] == 1
        feedback = await call(
            "teachonce_record_correction",
            skill_id=skill_id,
            feedback="Keep old drafts too",
            classification={"correction_type": "missing_rule", "analysis": "Preserve drafts"},
        )
        revision_context = await call("teachonce_revision_context", skill_id=skill_id)
        assert revision_context["corrections"][0]["id"] == feedback["id"]
        revised = {
            **proposal,
            "decision_rules": [
                *proposal["decision_rules"],
                {
                    "id": "drafts",
                    "condition": "Old draft",
                    "decision": "Keep old drafts",
                    "sources": [feedback["id"]],
                },
            ],
            "changes": [{"op": "add", "rule_id": "drafts", "description": "Preserve old drafts"}],
            "changelog": "Retain drafts",
        }
        candidate = await call(
            "teachonce_apply_correction",
            skill_id=skill_id,
            base_version=1,
            correction_ids=[feedback["id"]],
            proposal=revised,
        )
        assert candidate["status"] == "candidate"
        review = await call("teachonce_review_version", skill_id=skill_id, version=2)
        assert review["actual_diff"]
        assert review["candidate"]["derived_from"]["inference_sources"] == ["host-proposal"]
        assert store.get_skill(skill_id).active_version == 1

    asyncio.run(run())


def test_invalid_host_proposal_is_rejected_and_not_saved(store):
    from teachonce.learning import LearningEngine

    engine = LearningEngine(store)
    demo = engine.import_demonstration({"task_goal": "Group notes", "steps": [{"action": "Group"}]})
    with pytest.raises(SchemaValidationError):
        engine.extract_candidate_skill(
            "Courses", [demo.id], proposal={"decision_rules": "not rules"}
        )
    assert store.list_skills() == []


def test_user_can_fix_a_candidate_before_first_approval(store, reviewed_approval):
    from teachonce.correction import CorrectionEngine
    from teachonce.learning import LearningEngine

    learning = LearningEngine(store)
    demo = learning.import_demonstration(
        {"task_goal": "Group notes", "steps": [{"action": "Group"}]}
    )
    proposal = {
        "trigger_conditions": ["Course"],
        "decision_rules": [
            {"id": "base", "condition": "Known course", "decision": "Keep course folders"}
        ],
    }
    skill, _ = learning.extract_candidate_skill("Courses", [demo.id], proposal=proposal)
    engine = CorrectionEngine(store)
    feedback = engine.record_correction(
        skill.id,
        "Keep old drafts",
        classification={"correction_type": "missing_rule", "analysis": "Preserve drafts"},
    )
    version = engine.apply_correction(
        skill.id, base_version=1, correction_ids=[feedback.id], proposal=proposal
    )
    assert version.derived_from["activation_base"] == ["None"]
    reviewed_approval(engine, skill.id, version.version)
    assert store.get_active_version(skill.id).version == 2


def test_multiple_unapproved_revisions_keep_original_activation_baseline(store, reviewed_approval):
    from teachonce.correction import CorrectionEngine
    from teachonce.learning import LearningEngine

    learning = LearningEngine(store)
    demo = learning.import_demonstration(
        {"task_goal": "Group notes", "steps": [{"action": "Group"}]}
    )
    proposal = {
        "trigger_conditions": ["Course"],
        "decision_rules": [
            {"id": "base", "condition": "Known course", "decision": "Keep course folders"}
        ],
    }
    skill, _ = learning.extract_candidate_skill("Courses", [demo.id], proposal=proposal)
    engine = CorrectionEngine(store)
    reviewed_approval(engine, skill.id, 1)
    for base in (1, 2):
        feedback = engine.record_correction(
            skill.id,
            f"Review clarification {base}",
            classification={"correction_type": "other", "analysis": "Review"},
        )
        version = engine.apply_correction(
            skill.id, base_version=base, correction_ids=[feedback.id], proposal=proposal
        )
        assert version.derived_from["activation_base"] == ["1"]
    reviewed_approval(engine, skill.id, 3)
    assert store.get_active_version(skill.id).version == 3
