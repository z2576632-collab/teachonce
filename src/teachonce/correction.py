"""Correction engine — user feedback becomes a new skill version.

Design guarantees (docs/PRODUCT.md §4.3):
- History is append-only. A correction produces a NEW candidate version;
  nothing is overwritten. The active pointer only moves on explicit approval.
- Every revision carries a structured changelog ("changes": op + rule_id +
  description), so updates are explainable, reviewable, and reversible.
- Rollback moves the active pointer back to a previously approved version
  and marks the abandoned version as "rolled_back" — auditable, not hidden.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import (
    LLMNotConfiguredError,
    NotFoundError,
    SchemaValidationError,
    TeachOnceError,
    VersionStateError,
)
from .learning import validate_skill_payload
from .llm import LLMProvider, parse_json_response
from .models import Correction, Skill, SkillVersion
from .security import ensure_no_secrets
from .store import SkillStore

_VALID_CORRECTION_TYPES = {
    "wrong_decision",
    "missing_rule",
    "too_broad",
    "too_narrow",
    "other",
}

_RECORD_SYSTEM_PROMPT = """\
You are TeachOnce's correction intake.

Classify the user's feedback about a skill version into exactly one type:
- wrong_decision: the skill led to a wrong outcome in some case
- missing_rule: the skill lacks a rule for a situation the user cares about
- too_broad: a rule triggers in cases where it should not
- too_narrow: a rule is too specific and misses valid cases
- other

Also write a one-sentence analysis of what the user is asking to change.
Write in the SAME LANGUAGE as the feedback.
Respond with a single JSON object (no markdown fences):
{"correction_type": "...", "analysis": "..."}
"""

_APPLY_SYSTEM_PROMPT = """\
You are TeachOnce's correction-driven revision engine.

Input: the CURRENT skill version as JSON, plus one or more user CORRECTIONS.
Produce the REVISED skill version.

Hard rules:
- Apply ONLY changes grounded in the corrections. Keep all other rules as-is.
- Keep the "id" of kept and modified rules. For genuinely new rules use ids
  like "rule_new_1", "rule_new_2".
- If a correction is ambiguous or contradicts the evidence, do NOT guess:
  keep the current rule and add an item to open_questions.
- Never silently drop a rule. Removals must appear in "changes".
- "changes" must describe EVERY difference from the current version:
  {"op": "add" | "modify" | "remove", "rule_id": "...",
   "description": "<what changed and why>"}
- "changelog": one human-readable paragraph summarizing the revision.
- Keep the language of the current version's text.

Respond with a single JSON object (no markdown fences) with fields:
summary, trigger_conditions, decision_rules, exception_handling,
anti_patterns, open_questions, changelog, changes
"""


class CorrectionEngine:
    """Records user corrections and turns them into versioned skill updates.

    ``llm`` is optional at construction; recording and applying corrections
    require one. Approval and rollback are local-only operations.
    """

    def __init__(self, store: SkillStore, llm: LLMProvider | None = None) -> None:
        self.store = store
        self.llm = llm

    def _require_llm(self) -> LLMProvider:
        if self.llm is None:
            raise LLMNotConfiguredError(
                "Recording and applying corrections requires an LLM. "
                "Configure TEACHCONE_LLM_API_KEY (optionally "
                "TEACHCONE_LLM_BASE_URL / TEACHCONE_LLM_MODEL), then retry."
            )
        return self.llm

    # ------------------------------------------------------------------
    # record
    # ------------------------------------------------------------------

    def record_correction(
        self,
        skill_id: str,
        feedback: str,
        *,
        target_version: int | None = None,
    ) -> Correction:
        """Classify and store a natural-language correction."""
        feedback = feedback.strip()
        if not feedback:
            raise SchemaValidationError("Correction feedback must not be empty.")

        skill = self.store.get_skill(skill_id)

        if target_version is None:
            if skill.active_version is not None:
                target_version = skill.active_version
            else:
                versions = self.store.list_skill_versions(skill_id)
                if not versions:
                    raise NotFoundError(f"Skill '{skill_id}' has no versions to correct.")
                target_version = versions[-1].version
        else:
            self.store.get_skill_version(skill_id, target_version)  # existence

        ensure_no_secrets(feedback, where="correction")

        user_prompt = (
            f"# Skill being corrected\nname: {skill.name}\n"
            f"version: v{target_version}\n\n# User feedback\n{feedback}"
        )
        llm = self._require_llm()
        raw = llm.complete(_RECORD_SYSTEM_PROMPT, user_prompt, json_mode=True)
        data = parse_json_response(raw)

        ctype = str(data.get("correction_type", "other"))
        if ctype not in _VALID_CORRECTION_TYPES:
            ctype = "other"

        correction = Correction.create(
            skill_id=skill_id,
            target_version=target_version,
            feedback=feedback,
            correction_type=ctype,  # type: ignore[arg-type]
            analysis=str(data.get("analysis", "")).strip(),
        )
        self.store.add_correction(correction)
        return correction

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------

    def apply_correction(
        self,
        skill_id: str,
        *,
        base_version: int | None = None,
        correction_ids: list[str] | None = None,
    ) -> SkillVersion:
        """Produce a revised candidate version applying open corrections.

        ``base_version`` defaults to the active version, falling back to the
        latest existing version when nothing is approved yet.
        """
        skill = self.store.get_skill(skill_id)

        if correction_ids is not None:
            if len(correction_ids) != len(set(correction_ids)):
                raise SchemaValidationError("Correction ids must be unique; remove duplicates.")
            corrections = [self.store.get_correction(cid) for cid in correction_ids]
            if not corrections:
                raise TeachOnceError("No correction ids provided.")
        else:
            corrections = self.store.list_corrections(skill_id, status="open")
            if not corrections:
                raise TeachOnceError(
                    f"Skill '{skill.name}' has no open corrections. Record a "
                    "correction first (record_correction), then apply."
                )

        for correction in corrections:
            if correction.skill_id != skill_id:
                raise SchemaValidationError(
                    f"Correction '{correction.id}' belongs to another skill. "
                    "Choose corrections for the skill being revised."
                )
            if correction.status != "open":
                raise VersionStateError(
                    f"Correction '{correction.id}' was already applied in "
                    f"v{correction.applied_in_version}. Select open corrections or "
                    "record new feedback."
                )

        if base_version is None:
            base_version = skill.active_version
        if base_version is None:
            versions = self.store.list_skill_versions(skill_id)
            if not versions:
                raise NotFoundError(f"Skill '{skill_id}' has no versions.")
            base_version = versions[-1].version

        base_sv = self.store.get_skill_version(skill_id, base_version)

        user_prompt = _format_apply_prompt(base_sv, corrections)
        llm = self._require_llm()
        raw = llm.complete(_APPLY_SYSTEM_PROMPT, user_prompt, json_mode=True)
        data = parse_json_response(raw)

        correction_sources = list(
            dict.fromkeys(
                [*base_sv.derived_from.get("corrections", []), *(c.id for c in corrections)]
            )
        )
        valid_ids = set(base_sv.derived_from.get("demonstrations", [])) | set(correction_sources)
        cleaned = validate_skill_payload(data, valid_ids=valid_ids)

        changes = _validate_changes(data.get("changes"))
        changelog = str(data.get("changelog", "")).strip() or (
            f"Revised from v{base_version} based on {len(corrections)} user correction(s)."
        )

        sv = SkillVersion(
            skill_id=skill_id,
            version=0,  # Assigned atomically by save_revision after the LLM returns.
            trigger_conditions=cleaned["trigger_conditions"],
            decision_rules=cleaned["decision_rules"],
            exception_handling=cleaned["exception_handling"],
            anti_patterns=cleaned["anti_patterns"],
            open_questions=cleaned["open_questions"],
            summary=cleaned["summary"],
            changelog=changelog,
            changes=changes,
            status="candidate",
            derived_from={
                **{key: list(values) for key, values in base_sv.derived_from.items()},
                "demonstrations": list(base_sv.derived_from.get("demonstrations", [])),
                "corrections": correction_sources,
            },
        )
        return self.store.save_revision(sv, [c.id for c in corrections])

    # ------------------------------------------------------------------
    # approve / rollback
    # ------------------------------------------------------------------

    def approve_version(self, skill_id: str, version: int) -> Skill:
        """Human approval: mark version approved and make it the active one."""
        self.store.get_skill(skill_id)  # existence
        sv = self.store.get_skill_version(skill_id, version)  # existence
        self.store.set_version_status(skill_id, sv.version, "approved")
        self.store.set_active_version(skill_id, sv.version)
        return self.store.get_skill(skill_id)

    def rollback_to(self, skill_id: str, version: int) -> Skill:
        """Move the active pointer back to a previously approved version.

        The abandoned active version is marked ``rolled_back`` (auditable);
        its content is never modified.
        """
        skill = self.store.get_skill(skill_id)
        target = self.store.get_skill_version(skill_id, version)

        if target.status != "approved":
            raise VersionStateError(
                f"Cannot roll back to v{version}: its status is "
                f"'{target.status}'. Only previously human-approved versions "
                "can be rolled back to."
            )

        current = skill.active_version
        if current == version:
            return skill  # already active — nothing to do

        if current is not None:
            current_sv = self.store.get_skill_version(skill_id, current)
            if current_sv.status == "approved":
                self.store.set_version_status(skill_id, current, "rolled_back")

        self.store.set_active_version(skill_id, version)
        return self.store.get_skill(skill_id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _format_apply_prompt(base_sv: SkillVersion, corrections: list[Correction]) -> str:
    lines = [
        f"# Current skill version (v{base_sv.version})",
        json.dumps(base_sv.to_dict(), ensure_ascii=False, indent=2),
        f"\n# User corrections ({len(corrections)})",
    ]
    for c in corrections:
        lines.append(
            f"\n## correction id={c.id}"
            f"\ntype: {c.correction_type}"
            f"\nabout version: v{c.target_version}"
            f"\nfeedback: {c.feedback}"
        )
        if c.analysis:
            lines.append(f"analysis: {c.analysis}")
    return "\n".join(lines)


def _validate_changes(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        op = str(item.get("op", "")).strip().lower()
        if op not in ("add", "modify", "remove"):
            op = "other"
        out.append(
            {
                "op": op,
                "rule_id": str(item.get("rule_id", "")).strip(),
                "description": description,
            }
        )
    return out
