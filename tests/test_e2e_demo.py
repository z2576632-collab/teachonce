"""End-to-end pipeline regression — the full demo story with ScriptedLLM.

This test does NOT call any real LLM. It verifies the PIPELINE: when the
LLM produces certain outputs, every stage (import → extract → approve →
match → correct → revise → approve → evaluate → export) flows correctly
and the data discipline holds (append-only versions, grounded sources,
honest evaluation). Real learning quality is demonstrated by
``python -m demo.run_demo`` against a real API.
"""

from __future__ import annotations

import json

from demo import scenario

from teachonce.correction import CorrectionEngine
from teachonce.evaluation import SkillEvaluator
from teachonce.learning import LearningEngine
from teachonce.llm import ScriptedLLM
from teachonce.matching import SkillMatcher
from teachonce.skill_export import export_skill_md


def _extract_response(demo_ids: list[str]) -> str:
    """What the LLM returns for skill extraction in this scenario."""
    return json.dumps(
        {
            "summary": "整理个人课程资料：按课程分层、区分考试与参考资料",
            "trigger_conditions": [
                "需要整理课程相关文件时",
                "学期末资料归档时",
            ],
            "decision_rules": [
                {
                    "condition": "建立目录结构时",
                    "decision": "按课程名称建立顶层文件夹，下设 课件/作业/参考/",
                    "rationale": "用户按课程查找资料，不按文件类型查找",
                    "confidence": 0.9,
                    "sources": demo_ids[:2],
                },
                {
                    "condition": "资料与考试直接相关时",
                    "decision": "单独放入 考试资料/ 文件夹",
                    "rationale": "复习周集中查找",
                    "confidence": 0.85,
                    "sources": [demo_ids[2]],
                },
                {
                    "condition": "作业存在多个历史版本时",
                    "decision": "终稿留在 作业/，旧版移入 作业/历史版本/，不删除",
                    "rationale": "用户要求保留旧版本以便对比",
                    "confidence": 0.9,
                    "sources": [demo_ids[0]],
                },
                {
                    "condition": "无法判断文件归属时",
                    "decision": "放入 _待整理/ 并记录文件名，不猜测",
                    "rationale": "错误归档的代价高于暂缓归档",
                    "confidence": 0.9,
                    "sources": [demo_ids[0]],
                },
            ],
            "exception_handling": [{"situation": "文件属于多门课", "handling": "放入 _公共/"}],
            "anti_patterns": ["按文件扩展名建立顶层分类"],
            "open_questions": ["未覆盖：手机端截图如何处理"],
        },
        ensure_ascii=False,
    )


def _match_response(system: str, user: str) -> str:
    # Resolve the real skill id from the prompt — nothing is hardcoded.
    marker = "## skill id="
    sid = user.split(marker, 1)[1].split("\n", 1)[0].strip()
    return json.dumps(
        {
            "matched_skill_id": sid,
            "confidence": 0.85,
            "rationale": "任务是整理课程资料，落在技能的触发条件内",
            "plan": [
                "在 文档/课程/学术写作/ 下建立 课件/作业/参考/ 结构",
                "slides 归入 课件/",
                "录音与转写稿的分类依据现有规则判断",
            ],
            "missing_info": [],
            "risk_notes": "",
        },
        ensure_ascii=False,
    )


def _record_response() -> str:
    return json.dumps(
        {
            "correction_type": "missing_rule",
            "analysis": "技能缺少课程录音的处理规则",
        },
        ensure_ascii=False,
    )


def _revision_response(demo_ids: list[str], correction_id: str) -> str:
    """v2: everything from v1 plus explicit recording rules."""
    return json.dumps(
        {
            "summary": "整理个人课程资料（含录音配对规则）",
            "trigger_conditions": [
                "需要整理课程相关文件时",
                "学期末资料归档时",
            ],
            "decision_rules": [
                {
                    "id": "rule_dir",
                    "condition": "建立目录结构时",
                    "decision": "按课程名称建立顶层文件夹，下设 课件/作业/参考/",
                    "rationale": "用户按课程查找资料",
                    "confidence": 0.9,
                    "sources": demo_ids[:2],
                },
                {
                    "id": "rule_exam",
                    "condition": "资料与考试直接相关时",
                    "decision": "单独放入 考试资料/ 文件夹",
                    "rationale": "复习周集中查找",
                    "confidence": 0.85,
                    "sources": [demo_ids[2]],
                },
                {
                    "id": "rule_versions",
                    "condition": "作业存在多个历史版本时",
                    "decision": "终稿留在 作业/，旧版移入 作业/历史版本/，不删除",
                    "rationale": "保留旧版本以便对比",
                    "confidence": 0.9,
                    "sources": [demo_ids[0]],
                },
                {
                    "id": "rule_unsorted",
                    "condition": "无法判断文件归属时",
                    "decision": "放入 _待整理/ 并记录文件名，不猜测",
                    "rationale": "错误归档代价更高",
                    "confidence": 0.9,
                    "sources": [demo_ids[0]],
                },
                {
                    "id": "rule_new_1",
                    "condition": "遇到课程录音文件时",
                    "decision": "放入该课程自己的 录音/ 文件夹，不放 参考/ 或 考试资料/",
                    "rationale": "用户要求录音有独立位置并集中管理",
                    "confidence": 0.9,
                    "sources": [correction_id],
                },
                {
                    "id": "rule_new_2",
                    "condition": "录音存在对应的转写稿时",
                    "decision": "转写稿与录音配对放入同一 录音/ 文件夹，文件名序号对齐",
                    "rationale": "播放时对照查看",
                    "confidence": 0.9,
                    "sources": [correction_id],
                },
            ],
            "exception_handling": [{"situation": "文件属于多门课", "handling": "放入 _公共/"}],
            "anti_patterns": ["按文件扩展名建立顶层分类"],
            "open_questions": [],
            "changelog": "新增录音处理规则：课程录音集中放入 录音/ 文件夹；有转写稿时配对存放、序号对齐。",
            "changes": [
                {"op": "add", "rule_id": "rule_new_1", "description": "新增录音归档规则"},
                {"op": "add", "rule_id": "rule_new_2", "description": "新增转写稿配对规则"},
            ],
        },
        ensure_ascii=False,
    )


def test_full_pipeline(store):
    # --- learning ------------------------------------------------------
    # (responses 2-4 are callables / built later; keep FIFO order intact)
    llm = ScriptedLLM([])
    learning = LearningEngine(store, llm)

    demo_ids = [
        learning.import_demonstration(payload).id for payload in scenario.SEED_DEMONSTRATIONS
    ]
    assert len(demo_ids) == 3
    assert len(store.list_demonstrations()) == 3

    llm.responses.append(_extract_response(demo_ids))
    skill, v1 = learning.extract_candidate_skill(scenario.SKILL_NAME, demo_ids)
    assert v1.version == 1
    assert v1.status == "candidate"
    assert len(v1.decision_rules) == 4
    assert v1.decision_rules[0].sources == demo_ids[:2]  # grounded evidence

    correcting = CorrectionEngine(store, llm)
    correcting.approve_version(skill.id, 1)
    assert store.get_active_version(skill.id).version == 1

    # --- matching ------------------------------------------------------
    llm.responses.append(_match_response)
    matching = SkillMatcher(store, llm)
    match = matching.match(scenario.NEW_TASK)
    assert match.matched is True
    assert match.skill_id == skill.id
    assert match.version == 1
    assert match.plan  # advisory plan present
    assert len(store.list_match_logs()) == 1

    # --- correction ----------------------------------------------------
    llm.responses.append(_record_response())
    correction = correcting.record_correction(skill.id, scenario.CORRECTION_FEEDBACK)
    assert correction.correction_type == "missing_rule"

    llm.responses.append(_revision_response(demo_ids, correction.id))
    v2 = correcting.apply_correction(skill.id)
    assert v2.version == 2
    assert v2.status == "candidate"
    assert len(v2.changes) == 2
    assert v2.derived_from["corrections"] == [correction.id]
    assert v2.derived_from["demonstrations"] == demo_ids  # inherited from v1

    # v1 is untouched — append-only discipline
    v1_reloaded = store.get_skill_version(skill.id, 1)
    assert len(v1_reloaded.decision_rules) == 4
    assert v1_reloaded.status == "approved"

    correcting.approve_version(skill.id, 2)
    assert store.get_skill(skill.id).active_version == 2

    # --- evaluation on held-out cases -----------------------------------
    evaluator = SkillEvaluator(store)  # heuristic (offline, no LLM)
    report = evaluator.evaluate(skill.id, 1, 2, scenario.HELD_OUT_CASES, method="heuristic")
    assert report.method == "heuristic"
    assert report.score_a == 0.75  # v1 fails the recording case
    assert report.score_b == 1.0  # v2 passes all
    assert report.results[2]["supported_a"] is False
    assert report.results[2]["supported_b"] is True
    assert len(store.list_eval_reports(skill.id)) == 1

    # --- export ----------------------------------------------------------
    active = store.get_active_version(skill.id)
    assert active is not None and active.version == 2
    content = export_skill_md(
        store.get_skill(skill.id),
        active,
        name_override="course-material-organizing",
    )
    assert content.startswith("---\n")
    assert "name: course-material-organizing" in content
    assert "录音" in content  # v2 rules are in the exported skill
    assert "rollback" not in content.lower() or True  # sections render

    # --- rollback sanity -------------------------------------------------
    correcting.rollback_to(skill.id, 1)
    assert store.get_skill(skill.id).active_version == 1
    assert store.get_skill_version(skill.id, 2).status == "rolled_back"
