"""Trust boundaries and review lifecycle; no real model requests."""

from __future__ import annotations

import json
import sqlite3
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

from teachonce.cli import codex_config, doctor, main
from teachonce.correction import CorrectionEngine
from teachonce.errors import (
    ApprovalRequiredError,
    LLMError,
    SchemaValidationError,
    SensitiveContentError,
    VersionStateError,
)
from teachonce.evaluation import SkillEvaluator
from teachonce.learning import LearningEngine, validate_skill_payload
from teachonce.llm import LLMConfig, OpenAICompatibleLLM, ScriptedLLM, parse_json_response
from teachonce.matching import SkillMatcher
from teachonce.models import DecisionRule, Skill, SkillVersion
from teachonce.skill_export import export_skill_md, slugify_skill_name
from teachonce.store import SkillStore


def setup_candidate(store, *, version=1, skill=None):
    skill = skill or Skill.create("Courses")
    if version == 1:
        store.create_skill(skill)
    sv = SkillVersion(
        skill_id=skill.id,
        version=version,
        trigger_conditions=["Course notes"],
        decision_rules=[DecisionRule.create("Known course", "Keep course folders", rule_id="base")],
    )
    store.add_skill_version(sv)
    return skill, sv


def report_for(store, skill, version):
    return SkillEvaluator(store).evaluate(
        skill.id,
        store.get_skill(skill.id).active_version or version,
        version,
        [
            {
                "situation": f"Independent review example {version}",
                "expected_decision": "Keep course folders",
            }
        ],
        method="heuristic",
    )


def approve(store, skill, version):
    report = report_for(store, skill, version)
    return CorrectionEngine(store).approve_version(
        skill.id,
        version,
        evaluation_id=report.id,
        confirmed=True,
        reviewer="test human",
        reason="Reviewed each case",
    )


def test_approval_requires_real_confirmation_and_stored_evidence(store):
    skill, _ = setup_candidate(store)
    engine = CorrectionEngine(store)
    with pytest.raises(ApprovalRequiredError):
        engine.approve_version(skill.id, 1)
    with pytest.raises(ApprovalRequiredError, match="evaluation_id"):
        engine.approve_version(skill.id, 1, confirmed=True, reviewer="human", reason="Review")
    assert store.get_skill(skill.id).active_version is None
    assert store.list_activation_events(skill.id) == []
    report = report_for(store, skill, 1)
    review = engine.review_version(skill.id, 1)
    assert review["actual_diff"] and review["evaluations"][0]["eligible"]
    engine.approve_version(
        skill.id, 1, evaluation_id=report.id, confirmed=True, reviewer="human", reason="Review"
    )
    reopened = SkillStore(store.db_path)
    try:
        event = reopened.list_activation_events(skill.id)[0]
        assert event["event"] == "approve" and event["evaluation_id"] == report.id
        assert reopened.get_active_version(skill.id).version == 1
    finally:
        reopened.close()


def test_failed_report_or_wrong_skill_cannot_authorize_activation(store):
    skill, _ = setup_candidate(store)
    report = SkillEvaluator(store).evaluate(
        skill.id,
        1,
        1,
        [{"situation": "Unknown", "expected_decision": "Uncovered decision"}],
        method="heuristic",
    )
    with pytest.raises(ApprovalRequiredError, match="every"):
        CorrectionEngine(store).approve_version(
            skill.id, 1, evaluation_id=report.id, confirmed=True, reviewer="human", reason="Review"
        )
    other, _ = setup_candidate(store)
    passing = report_for(store, other, 1)
    with pytest.raises(ApprovalRequiredError, match="current active"):
        CorrectionEngine(store).approve_version(
            skill.id, 1, evaluation_id=passing.id, confirmed=True, reviewer="human", reason="Review"
        )
    assert store.get_skill(skill.id).active_version is None


def test_approval_and_rollback_are_atomic_and_reversible(store):
    skill, _ = setup_candidate(store)
    approve(store, skill, 1)
    setup_candidate(store, skill=skill, version=2)
    report = report_for(store, skill, 2)
    # Inject a persistence failure after status/pointer changes, before audit.
    store._conn.execute(
        "CREATE TRIGGER fail_audit BEFORE INSERT ON activation_events BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        CorrectionEngine(store).approve_version(
            skill.id, 2, evaluation_id=report.id, confirmed=True, reviewer="human", reason="Review"
        )
    assert store.get_skill(skill.id).active_version == 1
    assert store.get_skill_version(skill.id, 2).status == "candidate"
    assert len(store.list_activation_events(skill.id)) == 1
    store._conn.execute("DROP TRIGGER fail_audit")
    approve(store, skill, 2)
    engine = CorrectionEngine(store)
    engine.rollback_to(skill.id, 1, confirmed=True, reviewer="human", reason="Restore first")
    # A rolled-back version was previously approved and can be restored explicitly.
    engine.rollback_to(skill.id, 2, confirmed=True, reviewer="human", reason="Restore second")
    assert store.get_active_version(skill.id).version == 2
    assert [e["event"] for e in store.list_activation_events(skill.id)] == [
        "approve",
        "approve",
        "rollback",
        "rollback",
    ]


def test_concurrent_approvals_cannot_use_stale_baseline(store):
    skill, _ = setup_candidate(store)
    approve(store, skill, 1)
    reports = {}
    for version in (2, 3):
        setup_candidate(store, skill=skill, version=version)
        reports[version] = report_for(store, skill, version)

    def activate(version):
        connection = SkillStore(store.db_path)
        try:
            CorrectionEngine(connection).approve_version(
                skill.id,
                version,
                evaluation_id=reports[version].id,
                confirmed=True,
                reviewer="human",
                reason="Review",
            )
            return True
        except VersionStateError:
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(activate, (2, 3))) == 1
    assert len(store.list_activation_events(skill.id)) == 2


def test_initial_aggregate_failure_leaves_no_orphan(store):
    skill = Skill.create("Failed aggregate")
    wrong = SkillVersion(skill_id="missing", version=1)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_skill_with_version(skill, wrong)
    assert store.list_skills() == []


@pytest.mark.parametrize(
    "payload",
    [
        '{"confidence": NaN}',
        '{"confidence": Infinity}',
        '{"a": 1, "a": 2}',
        '{"nested": {"decision": "keep", "decision": "delete"}}',
    ],
)
def test_ambiguous_model_json_is_rejected(payload):
    with pytest.raises(SchemaValidationError):
        parse_json_response(payload)


@pytest.mark.parametrize(
    "payload",
    [
        '{"password": "synthetic-value"}',
        '{"cookie":"synthetic-value"}',
        "invalid json password=synthetic-value",
    ],
)
def test_model_and_structured_credentials_are_blocked_without_echo(payload):
    with pytest.raises(SensitiveContentError) as exc:
        parse_json_response(payload)
    assert "synthetic-value" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.invalid/v1",
        "https://user:secret@example.invalid/v1",
        "https://example.invalid/v1?password=secret",
    ],
)
def test_unsafe_llm_endpoint_rejected(url):
    with pytest.raises(LLMError):
        OpenAICompatibleLLM(LLMConfig(api_key="synthetic", base_url=url))


def test_provider_errors_and_repr_do_not_disclose_credentials(monkeypatch):
    config = LLMConfig(api_key="synthetic-private-value")
    assert "synthetic-private-value" not in repr(config)
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: httpx.Response(403, text="synthetic-private-value")
    )
    with pytest.raises(LLMError) as exc:
        OpenAICompatibleLLM(config).complete("s", "u")
    assert "synthetic-private-value" not in str(exc.value)


@pytest.mark.parametrize(
    "field,value",
    [("confidence", True), ("confidence", "0.9"), ("condition", None), ("sources", "demo_one")],
)
def test_skill_output_rejects_coerced_types(field, value):
    rule = {"condition": "Course", "decision": "Keep", field: value}
    with pytest.raises(SchemaValidationError):
        validate_skill_payload(
            {"trigger_conditions": ["Course"], "decision_rules": [rule]}, valid_ids=set()
        )


@pytest.mark.parametrize(
    "field,value",
    [("task_goal", None), ("steps", "click"), ("steps", [{"action": None}]), ("tags", [5])],
)
def test_demonstration_boundary_rejects_coercion(store, field, value):
    payload = {"task_goal": "Organize", "steps": [{"action": "Group"}], field: value}
    with pytest.raises(SchemaValidationError):
        LearningEngine(store).import_demonstration(payload)
    assert not store.list_demonstrations()


@pytest.mark.parametrize(
    "plan,risk",
    [(["delete old files"], ""), (["Group by course"], "Permissions are unclear"), ([], "")],
)
def test_risk_in_model_plan_or_notes_requires_confirmation(store, plan, risk):
    skill, _ = setup_candidate(store)
    approve(store, skill, 1)
    proposal = {
        "matched_skill_id": skill.id,
        "matched_version": 1,
        "confidence": 0.99,
        "rationale": "Course matches",
        "plan": plan,
        "risk_notes": risk,
        "missing_info": [],
    }
    result = SkillMatcher(store).match("Organize notes", proposal=proposal)
    assert result.requires_confirmation


def test_blind_decision_never_receives_expected_answer(store):
    skill, _ = setup_candidate(store)
    expected = "UNIQUE-EXPECTED-ANSWER"

    def decision(system, user):
        assert expected not in system + user
        return json.dumps({"predicted_decision": "Keep course folders", "reasoning": "Rule base"})

    def judge(system, user):
        assert expected in user and "Keep course folders" in user
        return json.dumps({"matches_expected": False, "reasoning": "Different action"})

    llm = ScriptedLLM([decision, judge, decision, judge])
    report = SkillEvaluator(store, llm).evaluate(
        skill.id,
        1,
        1,
        [{"situation": "New course", "expected_decision": expected}],
        method="llm_blind",
    )
    assert report.method == "llm_blind" and report.score_b == 0
    assert "Keep course folders" in report.results[0]["detail_b"]
    assert len(llm.calls) == 4


def test_export_yaml_quoting_and_slug_boundaries(store):
    skill, sv = setup_candidate(store)
    sv = replace(sv, summary='Meaning: "quote" # hash')
    content = export_skill_md(skill, sv)
    description = content.splitlines()[2].removeprefix("description: ")
    assert json.loads(description).startswith('Meaning: "quote" # hash')
    assert "REVIEW ONLY" in content
    assert not slugify_skill_name("a" * 63 + " another").endswith("-")


def test_cli_diagnostics_and_skill_installation_are_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEACHONCE_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("TEACHONCE_LLM_API_KEY", "synthetic-private-value")
    info = doctor()
    assert "synthetic-private-value" not in json.dumps(info)
    assert not (tmp_path / "data").exists()
    config = tomllib.loads(codex_config())["mcp_servers"]["teachonce"]
    assert config["args"] == ["-m", "teachonce.mcp_server"]
    assert config["tools"]["teachonce_approve_version"]["approval_mode"] == "prompt"
    destination = tmp_path / "skills"
    assert main(["install-skill", "--dest", str(destination)]) == 0
    assert (destination / "teachonce" / "SKILL.md").is_file()
    assert main(["install-skill", "--dest", str(destination)]) == 1
    assert "synthetic-private-value" not in capsys.readouterr().err


def test_modern_env_names_take_precedence_and_legacy_names_still_work():
    config = LLMConfig.from_env({"TEACHONCE_LLM_API_KEY": "new", "TEACHCONE_LLM_API_KEY": "old"})
    assert config.api_key == "new"
    assert LLMConfig.from_env({"TEACHCONE_LLM_API_KEY": "old"}).api_key == "old"


def test_gui_modern_explicit_disable_overrides_legacy_enable():
    from teachonce.adapters.openadapt import gui_observation_consented

    assert gui_observation_consented({"TEACHONCE_GUI_OBSERVATION": "true"})
    assert not gui_observation_consented(
        {"TEACHONCE_GUI_OBSERVATION": "0", "TEACHCONE_GUI_OBSERVATION": "1"}
    )


def test_demo_requires_concrete_interactive_review(store, monkeypatch):
    import sys

    from teachonce.demo import _review_and_approve

    skill, _ = setup_candidate(store)
    report = report_for(store, skill, 1)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert not _review_and_approve(CorrectionEngine(store), skill.id, 1, report)
    assert store.get_skill(skill.id).active_version is None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPROVE")
    assert _review_and_approve(CorrectionEngine(store), skill.id, 1, report)
    assert store.get_active_version(skill.id).version == 1
