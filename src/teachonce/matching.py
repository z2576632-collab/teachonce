"""Matching engine — does a new task fit one of the learned skills?

Safety-first design (docs/PRODUCT.md §4.4):
- A match NEVER executes anything. It selects a skill, explains why, and
  proposes a plan for the *host* agent to execute under its own permissions.
- Uncertainty routes to the human: low confidence, missing information, or
  risky wording (delete/overwrite/format...) forces ``requires_confirmation``.
- Hallucinated skill ids are rejected — a skill id that is not in the
  candidate catalog counts as "no match".
"""

from __future__ import annotations

import json
import math
from typing import Any

from .errors import LLMNotConfiguredError, SchemaValidationError
from .llm import LLMProvider, parse_json_response
from .models import MatchResult, Skill, SkillVersion
from .security import ensure_no_secrets
from .store import SkillStore

_MATCH_SYSTEM_PROMPT = """\
You are TeachOnce's skill matcher.

You receive a NEW TASK and a catalog of the user's learned skills (each with
trigger conditions, decision rules, and exceptions). Decide:

1. Does exactly one skill genuinely apply? If none genuinely applies, return
   null — do NOT force a match.
2. Confidence 0.0-1.0: is the task inside the skill's trigger conditions?
   Are key details missing that the rules need?
3. If a skill applies, outline a concrete PLAN: the specific decisions you
   would make per the skill's rules, as an advisory list. You never execute;
   the host agent executes after the user's permission.
4. List MISSING INFORMATION the user should provide before execution.

Write output text in the SAME LANGUAGE as the task.
Respond with a single JSON object (no markdown fences):

{
  "matched_skill_id": "<id from the catalog, or null>",
  "confidence": 0.0,
  "rationale": "<why this skill matches (or why none does)>",
  "plan": ["<decision/step grounded in the skill's rules>", "..."],
  "missing_info": ["..."],
  "risk_notes": "<anything risky or ambiguous, or empty string>"
}
"""


# Deterministic safety scan — never rely on the LLM for this.
_DANGER_KEYWORDS = [
    "删除",
    "删掉",
    "清除",
    "清空",
    "格式化",
    "覆盖",
    "永久删除",
    "彻底删",
    "rm -",
    "rm ",
    "del ",
    "delete",
    "remove file",
    "wipe",
    "erase",
    "overwrite",
    "format drive",
    "shred",
]


class SkillMatcher:
    """Matches a task description against learned skills (active versions only).

    ``llm`` is optional at construction; matching itself requires one.
    An empty skill catalog short-circuits without needing an LLM.
    """

    def __init__(
        self,
        store: SkillStore,
        llm: LLMProvider | None = None,
        *,
        confidence_threshold: float = 0.6,
    ) -> None:
        self.store = store
        self.llm = llm
        self.confidence_threshold = confidence_threshold

    def _require_llm(self) -> LLMProvider:
        if self.llm is None:
            raise LLMNotConfiguredError(
                "Skill matching requires an LLM. Configure "
                "TEACHCONE_LLM_API_KEY (optionally TEACHCONE_LLM_BASE_URL / "
                "TEACHCONE_LLM_MODEL), then retry."
            )
        return self.llm

    def match(
        self,
        task: str,
        *,
        skill_ids: list[str] | None = None,
        log: bool = True,
        proposal: dict[str, Any] | None = None,
    ) -> MatchResult:
        """Select a skill for ``task`` and explain the choice.

        Only skills with an approved active version are candidates.
        """
        if not isinstance(task, str):
            raise SchemaValidationError("Task description must be a string.")
        task = task.strip()
        if not task:
            raise SchemaValidationError("Task description must not be empty.")
        ensure_no_secrets(task, where="task")

        candidates: list[tuple[Skill, SkillVersion]] = []
        for skill in self.store.list_skills():
            if skill_ids is not None and skill.id not in skill_ids:
                continue
            active = self.store.get_active_version(skill.id)
            if active is not None and active.status == "approved":
                candidates.append((skill, active))

        if not candidates:
            result = MatchResult(
                task=task,
                matched=False,
                confidence=0.0,
                rationale=(
                    "No approved skills are available to match against. "
                    "Teach me a task first (import demonstrations and approve "
                    "the extracted skill)."
                ),
                requires_confirmation=True,
            )
            if log:
                self.store.log_match(result)
            return result

        user_prompt = _format_match_prompt(task, candidates)
        if proposal is None:
            llm = self._require_llm()
            raw = llm.complete(_MATCH_SYSTEM_PROMPT, user_prompt, json_mode=True)
        else:
            raw = json.dumps(proposal, ensure_ascii=False)
        data = parse_json_response(raw)

        by_id = {skill.id: (skill, sv) for skill, sv in candidates}

        matched_id = data.get("matched_skill_id")
        matched_id = str(matched_id) if matched_id else None

        confidence = data.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise SchemaValidationError(
                "Match confidence must be a finite JSON number from 0 to 1."
            )
        rationale = _text(data.get("rationale"), "rationale", required=True)
        plan = _str_list(data.get("plan"))
        missing_info = _str_list(data.get("missing_info"))
        risk_notes = _text(data.get("risk_notes", ""), "risk_notes")

        if matched_id is None or matched_id not in by_id:
            # Includes hallucinated ids — treated as no match, never forced.
            if matched_id is not None and rationale:
                rationale += f" (Note: matcher referenced unknown skill id '{matched_id}'; treated as no match.)"
            result = MatchResult(
                task=task,
                matched=False,
                confidence=confidence,
                rationale=rationale or "No learned skill applies to this task.",
                missing_info=missing_info,
                requires_confirmation=True,
            )
            if log:
                self.store.log_match(result)
            return result

        skill, active = by_id[matched_id]
        if proposal is not None and (
            type(proposal.get("matched_version")) is not int
            or proposal["matched_version"] != active.version
        ):
            raise SchemaValidationError(
                "Host proposal needs the current matched_version; refresh matching context."
            )

        flagged = _detect_risk(task + "\n" + "\n".join(plan))
        if flagged:
            risk_notes = (risk_notes + " " if risk_notes else "") + (
                f"Task wording mentions potentially destructive operations "
                f"({', '.join(flagged)}). Human confirmation required."
            )

        requires_confirmation = (
            confidence < self.confidence_threshold
            or bool(missing_info)
            or bool(flagged)
            or bool(risk_notes)
            or not plan
        )
        current = self.store.get_active_version(skill.id)
        if current is None or current.version != active.version or current.status != "approved":
            requires_confirmation = True
            plan = []
            missing_info.append("Skill activation changed while planning; match the task again.")

        result = MatchResult(
            task=task,
            matched=True,
            skill_id=skill.id,
            skill_name=skill.name,
            version=active.version,
            confidence=confidence,
            rationale=rationale,
            plan=plan,
            requires_confirmation=requires_confirmation,
            missing_info=missing_info,
        )
        if risk_notes:
            result.rationale = f"{result.rationale}\nRisk notes: {risk_notes}".strip()
        if log:
            self.store.log_match(result)
        return result

    def matching_context(self, task: str) -> dict[str, Any]:
        """Return the current approved catalog for host-model inference, without executing."""
        if not isinstance(task, str) or not task.strip():
            raise SchemaValidationError("Provide a non-empty task description.")
        ensure_no_secrets(task, where="task")
        candidates = []
        for skill in self.store.list_skills():
            active = self.store.get_active_version(skill.id)
            if active is not None and active.status == "approved":
                candidates.append((skill, active))
        prompt = _format_match_prompt(task, candidates)
        ensure_no_secrets(prompt, where="matching context")
        return {
            "system_prompt": _MATCH_SYSTEM_PROMPT,
            "user_prompt": prompt,
            "server_inference_performed": False,
            "execution_performed": False,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _format_match_prompt(task: str, candidates: list[tuple[Skill, SkillVersion]]) -> str:
    lines = [f"# New task\n{task}", f"\n# Learned-skill catalog ({len(candidates)})"]
    for skill, sv in candidates:
        lines.append(f"\n## skill id={skill.id}")
        lines.append(f"name: {skill.name}")
        lines.append(f"version: {sv.version}")
        summary = sv.summary or skill.description
        if summary:
            lines.append(f"summary: {summary}")
        lines.append("trigger_conditions:")
        for cond in sv.trigger_conditions:
            lines.append(f"  - {cond}")
        if sv.decision_rules:
            lines.append("decision_rules:")
            for rule in sv.decision_rules:
                lines.append(f"  - when {rule.condition} -> {rule.decision}")
        if sv.exception_handling:
            lines.append("exception_handling:")
            for exc in sv.exception_handling:
                lines.append(f"  - {exc.get('situation', '')} -> {exc.get('handling', '')}")
        if sv.anti_patterns:
            lines.append("anti_patterns:")
            for ap in sv.anti_patterns:
                lines.append(f"  - {ap}")
    return "\n".join(lines)


def _detect_risk(task: str) -> list[str]:
    lowered = task.lower()
    return [kw for kw in _DANGER_KEYWORDS if kw in lowered]


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(x, str) or not x.strip() for x in raw):
        raise SchemaValidationError(
            "Match plan and missing_info must be lists of non-empty strings."
        )
    return [x.strip() for x in raw]


def _text(raw: Any, field: str, *, required: bool = False) -> str:
    if not isinstance(raw, str) or (required and not raw.strip()):
        raise SchemaValidationError(
            f"Match '{field}' must be {'non-empty ' if required else ''}text."
        )
    return raw.strip()
