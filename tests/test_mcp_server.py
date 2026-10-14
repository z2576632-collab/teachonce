"""MCP server tests — tools invoked through the real MCP layer.

These exercise parameter schema validation, thread-safety of the store,
and the structured return values that MCP clients actually receive.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from teachonce.adapters.openadapt import CONSENT_ENV as GUI_CONSENT_ENV
from teachonce.errors import (
    AdapterUnavailableError,
    ConsentRequiredError,
    NotFoundError,
)
from teachonce.llm import OpenAICompatibleLLM, ScriptedLLM
from teachonce.mcp_server import build_server
from teachonce.models import Correction, Demonstration, EvalReport, Skill
from teachonce.store import SkillStore

ALL_TOOLS = {
    "teachonce_import_demonstration",
    "teachonce_generate_skill",
    "teachonce_list_demonstrations",
    "teachonce_list_corrections",
    "teachonce_list_evaluations",
    "teachonce_list_skills",
    "teachonce_get_skill",
    "teachonce_match_skill",
    "teachonce_record_correction",
    "teachonce_apply_correction",
    "teachonce_approve_version",
    "teachonce_rollback",
    "teachonce_evaluate_versions",
    "teachonce_export_skill_md",
    "teachonce_optimize_skill",
    "teachonce_import_gui_recording",
}


def _call(mcp, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke an MCP tool through the real protocol layer."""
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return json.loads(result.content[0].text)


def _tool_names(mcp) -> set[str]:
    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


def _extract_response() -> str:
    return json.dumps(
        {
            "summary": "整理课程资料",
            "trigger_conditions": ["整理课程文件时"],
            "decision_rules": [
                {
                    "condition": "归档时",
                    "decision": "按课程分文件夹",
                    "rationale": "按课查找",
                    "confidence": 0.9,
                    "sources": [],
                }
            ],
            "exception_handling": [],
            "anti_patterns": [],
            "open_questions": [],
        },
        ensure_ascii=False,
    )


def test_server_registers_all_tools(store):
    mcp = build_server(store=store, llm=ScriptedLLM([]))
    assert _tool_names(mcp) == ALL_TOOLS


def test_import_via_mcp(store):
    mcp = build_server(store=store, llm=ScriptedLLM([]))
    out = _call(
        mcp,
        "teachonce_import_demonstration",
        {
            "task_goal": "整理课程资料",
            "steps": [{"action": "建文件夹", "decision": "按课程分"}],
        },
    )
    assert out["demonstration_id"].startswith("demo_")
    assert out["steps_count"] == 1
    # persisted
    assert len(store.list_demonstrations()) == 1


def test_list_skills_empty(store):
    mcp = build_server(store=store, llm=ScriptedLLM([]))
    out = _call(mcp, "teachonce_list_skills")
    assert out == {"skills": [], "count": 0}


def test_generate_get_approve_export_flow(store):
    mcp = build_server(store=store, llm=ScriptedLLM([_extract_response()]))

    demo = _call(
        mcp,
        "teachonce_import_demonstration",
        {"task_goal": "整理资料", "steps": [{"action": "a", "decision": "d"}]},
    )
    gen = _call(
        mcp,
        "teachonce_generate_skill",
        {
            "name": "课程资料整理",
            "demonstration_ids": [demo["demonstration_id"]],
        },
    )
    assert gen["skill_id"].startswith("skill_")
    assert gen["status"] == "candidate"
    assert gen["version"] == 1

    # candidate is not active yet
    detail = _call(mcp, "teachonce_get_skill", {"skill_id": gen["skill_id"]})
    assert detail["active_version"] is None

    # export before approval fails
    try:
        _call(mcp, "teachonce_export_skill_md", {"skill_id": gen["skill_id"]})
        raise AssertionError("export should fail without an approved version")
    except Exception as exc:
        assert "approve" in str(exc).lower() or "error" in str(exc).lower()

    approved = _call(mcp, "teachonce_approve_version", {"skill_id": gen["skill_id"], "version": 1})
    assert approved["active_version"] == 1

    exported = _call(
        mcp,
        "teachonce_export_skill_md",
        {"skill_id": gen["skill_id"], "name_override": "course-organizing"},
    )
    assert exported["filename"] == "SKILL.md"
    assert exported["content"].startswith("---\n")
    assert "name: course-organizing" in exported["content"]

    listed = _call(mcp, "teachonce_list_skills")
    assert listed["count"] == 1
    assert listed["skills"][0]["versions"][0]["status"] == "approved"


def test_match_correction_rollback_flow(store):
    def match_response(system: str, user: str) -> str:
        # resolve the real skill id from the prompt (no hardcoding possible)
        marker = "## skill id="
        sid = user.split(marker, 1)[1].split("\n", 1)[0].strip()
        return json.dumps(
            {
                "matched_skill_id": sid,
                "confidence": 0.9,
                "rationale": "任务与技能匹配",
                "plan": ["按课程建目录"],
                "missing_info": [],
                "risk_notes": "",
            },
            ensure_ascii=False,
        )

    record_response = json.dumps(
        {"correction_type": "missing_rule", "analysis": "缺录音规则"},
        ensure_ascii=False,
    )
    revision_response = json.dumps(
        {
            "summary": "整理课程资料（含录音）",
            "trigger_conditions": ["整理课程文件时"],
            "decision_rules": [
                {
                    "id": "r1",
                    "condition": "归档时",
                    "decision": "按课程分文件夹",
                    "rationale": "按课查找",
                    "confidence": 0.9,
                    "sources": [],
                },
                {
                    "id": "rule_new_1",
                    "condition": "遇到录音时",
                    "decision": "放 录音/ 目录",
                    "rationale": "用户要求",
                    "confidence": 0.9,
                    "sources": [],
                },
            ],
            "exception_handling": [],
            "anti_patterns": [],
            "open_questions": [],
            "changelog": "新增录音规则",
            "changes": [{"op": "add", "rule_id": "rule_new_1", "description": "新增录音规则"}],
        },
        ensure_ascii=False,
    )

    llm = ScriptedLLM([_extract_response(), match_response, record_response, revision_response])
    mcp = build_server(store=store, llm=llm)

    demo = _call(
        mcp,
        "teachonce_import_demonstration",
        {"task_goal": "g", "steps": [{"action": "a"}]},
    )
    gen = _call(
        mcp,
        "teachonce_generate_skill",
        {"name": "s", "demonstration_ids": [demo["demonstration_id"]]},
    )
    skill_id = gen["skill_id"]
    _call(mcp, "teachonce_approve_version", {"skill_id": skill_id, "version": 1})

    matched = _call(mcp, "teachonce_match_skill", {"task": "整理学术写作课资料"})
    assert matched["matched"] is True
    assert matched["skill_id"] == skill_id
    assert matched["requires_confirmation"] is False

    correction = _call(
        mcp,
        "teachonce_record_correction",
        {"skill_id": skill_id, "feedback": "录音要单独放录音文件夹"},
    )
    assert correction["correction_type"] == "missing_rule"

    applied = _call(mcp, "teachonce_apply_correction", {"skill_id": skill_id})
    assert applied["version"] == 2
    assert applied["status"] == "candidate"
    assert applied["changes"][0]["op"] == "add"

    _call(mcp, "teachonce_approve_version", {"skill_id": skill_id, "version": 2})
    rolled = _call(mcp, "teachonce_rollback", {"skill_id": skill_id, "version": 1})
    assert rolled["active_version"] == 1

    detail = _call(mcp, "teachonce_get_skill", {"skill_id": skill_id, "version": 2})
    assert detail["requested_version"]["status"] == "rolled_back"


def test_evaluate_via_mcp_heuristic(store):
    mcp = build_server(store=store, llm=ScriptedLLM([_extract_response()]))
    demo = _call(
        mcp,
        "teachonce_import_demonstration",
        {"task_goal": "g", "steps": [{"action": "a"}]},
    )
    gen = _call(
        mcp,
        "teachonce_generate_skill",
        {"name": "s", "demonstration_ids": [demo["demonstration_id"]]},
    )
    skill_id = gen["skill_id"]

    out = _call(
        mcp,
        "teachonce_evaluate_versions",
        {
            "skill_id": skill_id,
            "version_a": 1,
            "version_b": 1,
            "cases": [
                {
                    "situation": "归档时",
                    "expected_decision": "按课程分文件夹",
                    "keywords": ["按课程"],
                }
            ],
            "method": "heuristic",
        },
    )
    assert out["method"] == "heuristic"
    assert out["score_a"] == 1.0


def test_saved_history_is_available_after_restart_without_llm(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    original = SkillStore(db_path)
    skill = Skill.create("Saved skill")
    demo = Demonstration.create("Organize notes", steps=[{"action": "Sort by course"}])
    open_feedback = Correction.create(skill.id, 1, "Keep recordings together")
    applied_feedback = Correction.create(skill.id, 1, "Preserve older files")
    report = EvalReport.create(
        skill.id,
        1,
        2,
        "heuristic",
        0.0,
        1.0,
        results=[{"supported_a": False, "supported_b": True}],
        notes="Keyword overlap only, not semantic understanding.",
    )
    try:
        original.add_demonstration(demo)
        original.create_skill(skill)
        original.add_correction(open_feedback)
        original.add_correction(applied_feedback)
        original.mark_correction_applied(applied_feedback.id, 2)
        original.save_eval_report(report)
    finally:
        original.close()

    def unexpected_llm(*args, **kwargs):
        pytest.fail("History tools must not resolve or call an LLM")

    monkeypatch.setattr(OpenAICompatibleLLM, "from_env", unexpected_llm)
    reopened = SkillStore(db_path)
    try:
        mcp = build_server(store=reopened)
        demos = _call(mcp, "teachonce_list_demonstrations")
        assert demos == {"demonstrations": [demo.to_dict()], "count": 1}
        pending = _call(mcp, "teachonce_list_corrections", {"skill_id": skill.id, "status": "open"})
        assert pending == {"corrections": [open_feedback.to_dict()], "count": 1}
        applied = _call(
            mcp, "teachonce_list_corrections", {"skill_id": skill.id, "status": "applied"}
        )
        assert applied["count"] == 1
        assert applied["corrections"][0]["applied_in_version"] == 2
        all_feedback = _call(mcp, "teachonce_list_corrections", {"skill_id": skill.id})
        assert all_feedback["count"] == 2
        reports = _call(mcp, "teachonce_list_evaluations", {"skill_id": skill.id})
        assert reports == {"reports": [report.to_dict()], "count": 1}
    finally:
        reopened.close()


@pytest.mark.parametrize("tool", ["teachonce_list_corrections", "teachonce_list_evaluations"])
def test_history_tools_report_unknown_skill(store, tool):
    mcp = build_server(store=store)
    with pytest.raises(ToolError) as exc:
        _call(mcp, tool, {"skill_id": "skill_missing"})
    assert isinstance(exc.value.__cause__, NotFoundError)


def test_list_corrections_rejects_unknown_status(store):
    skill = Skill.create("Skill")
    store.create_skill(skill)
    mcp = build_server(store=store)
    with pytest.raises(ToolError):
        _call(mcp, "teachonce_list_corrections", {"skill_id": skill.id, "status": "typo"})


def test_history_tools_return_empty_collections(store):
    skill = Skill.create("Skill")
    store.create_skill(skill)
    mcp = build_server(store=store)
    assert _call(mcp, "teachonce_list_demonstrations") == {"demonstrations": [], "count": 0}
    assert _call(mcp, "teachonce_list_corrections", {"skill_id": skill.id}) == {
        "corrections": [],
        "count": 0,
    }
    assert _call(mcp, "teachonce_list_evaluations", {"skill_id": skill.id}) == {
        "reports": [],
        "count": 0,
    }


# ----------------------------------------------------------------------
# adapter tools (GEPA optimization + OpenAdapt GUI recording import)
# ----------------------------------------------------------------------


def test_gui_recording_tool_requires_consent(store, monkeypatch):
    monkeypatch.delenv(GUI_CONSENT_ENV, raising=False)
    mcp = build_server(store=store)
    with pytest.raises(ToolError) as exc:
        _call(mcp, "teachonce_import_gui_recording", {"recording_path": "x.json"})
    assert isinstance(exc.value.__cause__, ConsentRequiredError)


def test_gui_recording_tool_imports_after_consent(store, tmp_path, monkeypatch):
    monkeypatch.setenv(GUI_CONSENT_ENV, "1")
    recording = tmp_path / "rec.json"
    recording.write_text(
        json.dumps(
            {
                "name": "整理课程资料",
                "events": [{"type": "click", "target": "下载文件夹"}],
            }
        ),
        encoding="utf-8",
    )
    mcp = build_server(store=store)
    out = _call(
        mcp,
        "teachonce_import_gui_recording",
        {"recording_path": str(recording)},
    )
    assert out["task_goal"] == "整理课程资料"
    assert _call(mcp, "teachonce_list_demonstrations")["count"] == 1


def test_optimize_tool_reports_missing_gepa_package(store, monkeypatch):
    from teachonce.adapters import gepa_optimizer
    from teachonce.models import DecisionRule, SkillVersion

    monkeypatch.setattr(gepa_optimizer, "gepa_available", lambda: False)
    mcp = build_server(store=store, llm=ScriptedLLM([]))
    skill = Skill.create("Skill")
    store.create_skill(skill)
    store.add_skill_version(
        SkillVersion(
            skill_id=skill.id,
            version=1,
            trigger_conditions=["触发条件"],
            decision_rules=[
                DecisionRule.create("条件", "决策", rationale="理由", rule_id="rule_1")
            ],
            status="approved",
        )
    )
    store.set_active_version(skill.id, 1)
    with pytest.raises(ToolError) as exc:
        _call(
            mcp,
            "teachonce_optimize_skill",
            {
                "skill_id": skill.id,
                "cases": [{"situation": "s", "expected_decision": "d"}],
            },
        )
    assert isinstance(exc.value.__cause__, AdapterUnavailableError)
