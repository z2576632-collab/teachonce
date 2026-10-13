"""MCP server tests — tools invoked through the real MCP layer.

These exercise parameter schema validation, thread-safety of the store,
and the structured return values that MCP clients actually receive.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from teachonce.llm import ScriptedLLM
from teachonce.mcp_server import build_server

ALL_TOOLS = {
    "teachonce_import_demonstration",
    "teachonce_generate_skill",
    "teachonce_list_skills",
    "teachonce_get_skill",
    "teachonce_match_skill",
    "teachonce_record_correction",
    "teachonce_apply_correction",
    "teachonce_approve_version",
    "teachonce_rollback",
    "teachonce_evaluate_versions",
    "teachonce_export_skill_md",
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
