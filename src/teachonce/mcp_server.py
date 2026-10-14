"""MCP server — exposes TeachOnce's learning capabilities to any agent.

Tools are thin orchestration over the engines (no business logic here,
see AGENTS.md §2). The LLM is resolved lazily so that local-only tools
(list / get / approve / rollback) work without an API key; LLM-backed
tools raise :class:`LLMNotConfiguredError` with fix instructions.

Run:  ``teachonce-mcp`` or ``python -m teachonce.mcp_server``
Transport: stdio. Built on the official MCP Python SDK (v2 ``MCPServer``,
formerly ``FastMCP``).
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from .correction import CorrectionEngine
from .errors import LLMNotConfiguredError
from .evaluation import SkillEvaluator
from .learning import LearningEngine
from .llm import LLMProvider, OpenAICompatibleLLM
from .matching import SkillMatcher
from .skill_export import export_skill_md
from .store import SkillStore


def build_server(
    store: SkillStore | None = None,
    llm: LLMProvider | None = None,
) -> MCPServer:
    """Build the TeachOnce MCP server.

    ``store`` / ``llm`` are injectable for tests and embedding; defaults
    come from the local database and environment configuration.
    """
    the_store = store if store is not None else SkillStore()
    llm_holder: dict[str, LLMProvider | None] = {"llm": llm}

    def get_llm() -> LLMProvider:
        if llm_holder["llm"] is None:
            llm_holder["llm"] = OpenAICompatibleLLM.from_env()
        return llm_holder["llm"]

    mcp = MCPServer("teachonce")

    # ------------------------------------------------------------------
    # learning loop
    # ------------------------------------------------------------------

    @mcp.tool()
    def teachonce_import_demonstration(
        task_goal: str,
        steps: list[dict[str, Any]] | None = None,
        context: str = "",
        key_judgments: list[dict[str, Any]] | None = None,
        outcome: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Import one structured demonstration of how the user completed a task.

        Each step: {"action": ..., "target": ..., "decision": ..., "rationale": ...}
        Each key judgment: {"situation": ..., "decision": ..., "why": ...}
        Secrets (passwords/tokens/cookies) are rejected at import time.
        Returns {"demonstration_id": ...}.
        """
        engine = LearningEngine(the_store)  # import is local-only, no LLM
        demo = engine.import_demonstration(
            {
                "task_goal": task_goal,
                "steps": steps or [],
                "context": context,
                "key_judgments": key_judgments or [],
                "outcome": outcome,
                "tags": tags or [],
            }
        )
        return {
            "demonstration_id": demo.id,
            "task_goal": demo.task_goal,
            "steps_count": len(demo.steps),
            "created_at": demo.created_at,
        }

    @mcp.tool()
    def teachonce_generate_skill(
        name: str,
        demonstration_ids: list[str],
        description: str = "",
    ) -> dict[str, Any]:
        """Extract a candidate skill (trigger conditions, decision rules,
        exceptions) from one or more imported demonstrations.

        The result is a CANDIDATE version: review it, then call
        teachonce_approve_version to make it active.
        """
        engine = LearningEngine(the_store, get_llm())
        skill, sv = engine.extract_candidate_skill(name, demonstration_ids, description=description)
        return {
            "skill_id": skill.id,
            "name": skill.name,
            "version": sv.version,
            "status": sv.status,
            "summary": sv.summary,
            "trigger_conditions": sv.trigger_conditions,
            "decision_rules": [r.to_dict() for r in sv.decision_rules],
            "exception_handling": sv.exception_handling,
            "anti_patterns": sv.anti_patterns,
            "open_questions": sv.open_questions,
            "changelog": sv.changelog,
        }

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    @mcp.tool()
    def teachonce_list_demonstrations() -> dict[str, Any]:
        """Retrieve saved demonstrations and their ids to resume learning.

        Local-only: no LLM or API key is needed. Use the returned ids with
        teachonce_generate_skill, including after restarting the server.
        """
        demonstrations = [demo.to_dict() for demo in the_store.list_demonstrations()]
        return {"demonstrations": demonstrations, "count": len(demonstrations)}

    @mcp.tool()
    def teachonce_list_corrections(
        skill_id: str, status: Literal["open", "applied"] | None = None
    ) -> dict[str, Any]:
        """Retrieve a skill's saved feedback without an LLM.

        Use status='open' to find feedback awaiting revision; omit it for
        the full history, including the version where feedback was applied.
        """
        the_store.get_skill(skill_id)
        corrections = [
            correction.to_dict()
            for correction in the_store.list_corrections(skill_id, status=status)
        ]
        return {"corrections": corrections, "count": len(corrections)}

    @mcp.tool()
    def teachonce_list_evaluations(skill_id: str) -> dict[str, Any]:
        """Retrieve saved version evaluations without rerunning or using an LLM.

        Includes per-case verdicts, evaluation method and limitations so
        the user can review evidence before approving a candidate version.
        """
        the_store.get_skill(skill_id)
        reports = [report.to_dict() for report in the_store.list_eval_reports(skill_id)]
        return {"reports": reports, "count": len(reports)}

    @mcp.tool()
    def teachonce_list_skills() -> dict[str, Any]:
        """List all learned skills with their active version (if approved)."""
        skills: list[dict[str, Any]] = []
        for skill in the_store.list_skills():
            versions = the_store.list_skill_versions(skill.id)
            skills.append(
                {
                    **skill.to_dict(),
                    "versions": [
                        {
                            "version": v.version,
                            "status": v.status,
                            "created_at": v.created_at,
                        }
                        for v in versions
                    ],
                }
            )
        return {"skills": skills, "count": len(skills)}

    @mcp.tool()
    def teachonce_get_skill(skill_id: str, version: int | None = None) -> dict[str, Any]:
        """Get a skill's details. Without ``version``: metadata + all version
        summaries + full content of the active version (if any). With
        ``version``: full content of that specific version."""
        skill = the_store.get_skill(skill_id)
        all_versions = the_store.list_skill_versions(skill_id)
        result: dict[str, Any] = {
            "skill": skill.to_dict(),
            "versions": [
                {
                    "version": v.version,
                    "status": v.status,
                    "changelog": v.changelog,
                    "created_at": v.created_at,
                }
                for v in all_versions
            ],
        }
        if version is not None:
            result["requested_version"] = the_store.get_skill_version(skill_id, version).to_dict()
        else:
            active = the_store.get_active_version(skill_id)
            result["active_version"] = active.to_dict() if active else None
        return result

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------

    @mcp.tool()
    def teachonce_match_skill(task: str, skill_ids: list[str] | None = None) -> dict[str, Any]:
        """Match a new task against learned skills.

        Returns the best match with confidence, rationale, an advisory plan
        and whether human confirmation is required. This NEVER executes the
        task — the host agent executes after the user's permission.
        """
        matcher = SkillMatcher(the_store, get_llm())
        result = matcher.match(task, skill_ids=skill_ids)
        return result.to_dict()

    # ------------------------------------------------------------------
    # corrections / versions
    # ------------------------------------------------------------------

    @mcp.tool()
    def teachonce_record_correction(
        skill_id: str,
        feedback: str,
        target_version: int | None = None,
    ) -> dict[str, Any]:
        """Record a natural-language correction about a skill version.

        The feedback is classified (wrong_decision / missing_rule /
        too_broad / too_narrow / other) and stored as an open correction.
        Apply it later with teachonce_apply_correction.
        """
        engine = CorrectionEngine(the_store, get_llm())
        correction = engine.record_correction(skill_id, feedback, target_version=target_version)
        return correction.to_dict()

    @mcp.tool()
    def teachonce_apply_correction(
        skill_id: str,
        base_version: int | None = None,
        correction_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Apply open corrections and produce a REVISED candidate version.

        History is append-only: the new version carries a structured
        changelog; the old versions stay intact. Review the changelog, then
        approve with teachonce_approve_version.
        """
        engine = CorrectionEngine(the_store, get_llm())
        sv = engine.apply_correction(
            skill_id, base_version=base_version, correction_ids=correction_ids
        )
        return {
            "skill_id": sv.skill_id,
            "version": sv.version,
            "status": sv.status,
            "changelog": sv.changelog,
            "changes": sv.changes,
            "decision_rules": [r.to_dict() for r in sv.decision_rules],
            "open_questions": sv.open_questions,
        }

    @mcp.tool()
    def teachonce_approve_version(skill_id: str, version: int) -> dict[str, Any]:
        """Human approval: mark a candidate version as approved and make it
        the active version used by matching."""
        engine = CorrectionEngine(the_store)
        skill = engine.approve_version(skill_id, version)
        return {
            "skill_id": skill.id,
            "name": skill.name,
            "active_version": skill.active_version,
            "updated_at": skill.updated_at,
        }

    @mcp.tool()
    def teachonce_rollback(skill_id: str, version: int) -> dict[str, Any]:
        """Roll the active pointer back to a previously approved version.

        The abandoned active version is marked 'rolled_back'; nothing is
        deleted and its content never changes.
        """
        engine = CorrectionEngine(the_store)
        skill = engine.rollback_to(skill_id, version)
        return {
            "skill_id": skill.id,
            "name": skill.name,
            "active_version": skill.active_version,
            "updated_at": skill.updated_at,
        }

    # ------------------------------------------------------------------
    # evaluation / export
    # ------------------------------------------------------------------

    @mcp.tool()
    def teachonce_evaluate_versions(
        skill_id: str,
        version_a: int,
        version_b: int,
        cases: list[dict[str, Any]],
        method: str = "auto",
    ) -> dict[str, Any]:
        """Compare two versions on held-out cases (never used for learning).

        Each case: {"situation": ..., "expected_decision": ...,
        "keywords": [...]} (keywords optional; used by the offline
        heuristic). method: "auto" | "llm_judge" | "heuristic". The report
        records the method and its limitations.
        """
        evaluator = SkillEvaluator(the_store)
        if method in ("auto", "llm_judge"):
            # Resolve lazily; "auto" falls back to the offline heuristic
            # when no LLM is configured, while "llm_judge" must raise.
            try:
                evaluator.llm = get_llm()
            except LLMNotConfiguredError:
                if method == "llm_judge":
                    raise
                evaluator.llm = None
        report = evaluator.evaluate(skill_id, version_a, version_b, cases, method=method)
        return report.to_dict()

    @mcp.tool()
    def teachonce_export_skill_md(
        skill_id: str,
        version: int | None = None,
        name_override: str | None = None,
    ) -> dict[str, Any]:
        """Export a skill version as a standard Agent Skills SKILL.md.

        ``name_override`` provides an ascii slug for the frontmatter when
        the skill name is not ascii (e.g. Chinese).
        """
        skill = the_store.get_skill(skill_id)
        if version is None:
            sv = the_store.get_active_version(skill_id)
            if sv is None:
                raise ValueError(
                    f"Skill '{skill.id}' has no approved active version. "
                    "Approve a version first, or pass an explicit 'version'."
                )
        else:
            sv = the_store.get_skill_version(skill_id, version)
        content = export_skill_md(skill, sv, name_override=name_override)
        return {
            "skill_id": skill.id,
            "version": sv.version,
            "filename": "SKILL.md",
            "content": content,
        }

    return mcp


def main() -> None:
    """Entry point for ``teachonce-mcp`` (stdio transport)."""
    build_server().run()


if __name__ == "__main__":
    main()
