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
    ApprovalRequiredError,
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
        classification: dict[str, Any] | None = None,
    ) -> Correction:
        """Classify and store a natural-language correction."""
        if not isinstance(feedback, str):
            raise SchemaValidationError("Correction feedback must be text.")
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
        if classification is None:
            llm = self._require_llm()
            raw = llm.complete(_RECORD_SYSTEM_PROMPT, user_prompt, json_mode=True)
        else:
            raw = json.dumps(classification, ensure_ascii=False)
        data = parse_json_response(raw)

        ctype = str(data.get("correction_type", "other"))
        if ctype not in _VALID_CORRECTION_TYPES:
            ctype = "other"

        analysis = data.get("analysis", "")
        if not isinstance(analysis, str):
            raise SchemaValidationError("Correction analysis must be text.")
        correction = Correction.create(
            skill_id=skill_id,
            target_version=target_version,
            feedback=feedback,
            correction_type=ctype,  # type: ignore[arg-type]
            analysis=("[host proposal] " if classification is not None else "") + analysis.strip(),
        )
        self.store.add_correction(correction)
        return correction

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------

    def revision_context(
        self,
        skill_id: str,
        *,
        base_version: int | None = None,
        correction_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load and validate pending corrections for host-model revision.

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
        ensure_no_secrets(user_prompt, where="revision context")
        return {
            "system_prompt": _APPLY_SYSTEM_PROMPT,
            "user_prompt": user_prompt,
            "base": base_sv.to_dict(),
            "corrections": [c.to_dict() for c in corrections],
            "server_inference_performed": False,
            "activation_base": list(
                base_sv.derived_from.get("activation_base", [str(skill.active_version)])
            )
            if base_sv.status == "candidate"
            else [str(base_sv.version if skill.active_version is not None else None)],
        }

    def apply_correction(
        self,
        skill_id: str,
        *,
        base_version: int | None = None,
        correction_ids: list[str] | None = None,
        proposal: dict[str, Any] | None = None,
    ) -> SkillVersion:
        """Validate API or explicit host inference and atomically save a candidate revision."""
        if proposal is not None and (base_version is None or correction_ids is None):
            raise SchemaValidationError(
                "Host revision proposals require explicit base_version and correction_ids from revision_context."
            )
        context = self.revision_context(
            skill_id, base_version=base_version, correction_ids=correction_ids
        )
        base_sv = SkillVersion.from_dict(context["base"])
        base_version = base_sv.version
        corrections = [Correction.from_dict(c) for c in context["corrections"]]
        if proposal is None:
            llm = self._require_llm()
            raw = llm.complete(_APPLY_SYSTEM_PROMPT, context["user_prompt"], json_mode=True)
        else:
            raw = json.dumps(proposal, ensure_ascii=False)
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
                "base_versions": [str(base_version)],
                "activation_base": context["activation_base"],
                **(
                    {
                        "inference_sources": list(
                            dict.fromkeys(
                                [
                                    *base_sv.derived_from.get("inference_sources", []),
                                    "host-proposal",
                                ]
                            )
                        )
                    }
                    if proposal is not None
                    else {}
                ),
            },
        )
        return self.store.save_revision(sv, [c.id for c in corrections])

    # ------------------------------------------------------------------
    # approve / rollback
    # ------------------------------------------------------------------

    def review_version(self, skill_id: str, version: int) -> dict[str, Any]:
        """Prepare candidate content, report eligibility and review history without a model."""
        skill = self.store.get_skill(skill_id)
        sv = self.store.get_skill_version(skill_id, version)
        reports = []
        for report in self.store.list_eval_reports(skill_id):
            if report.version_b != version:
                continue
            reason = self._report_issue(skill, sv, report)
            reports.append({**report.to_dict(), "eligible": reason is None, "issue": reason})
        return {
            "skill": skill.to_dict(),
            "candidate": sv.to_dict(),
            "baseline_version": skill.active_version,
            "actual_diff": _actual_diff(self.store.get_active_version(skill_id), sv),
            "evaluations": reports,
            "activation_history": self.store.list_activation_events(skill_id),
            "confirmation_notice": "The host must obtain actual human approval; a flag is not identity verification.",
        }

    @staticmethod
    def _report_issue(skill: Skill, sv: SkillVersion, report: Any) -> str | None:
        baseline = skill.active_version if skill.active_version is not None else sv.version
        if sv.status != "candidate":
            return (
                "Only a candidate can be approved; use rollback for previously approved versions."
            )
        bases = sv.derived_from.get("activation_base", [])
        if bases and bases != [str(skill.active_version)]:
            return (
                "Candidate was based on another active version. Rebase its learning before review."
            )
        if report.skill_id != skill.id or (report.version_a, report.version_b) != (
            baseline,
            sv.version,
        ):
            return "Evaluation must compare the current active version with this candidate (or v1 with itself)."
        if not report.results or any(
            type(r.get("supported_a")) is not bool or type(r.get("supported_b")) is not bool
            for r in report.results
        ):
            return "Evaluation lacks valid per-case boolean verdicts. Run a fresh evaluation."
        if any(not r["supported_b"] for r in report.results):
            return "Candidate must pass every reviewed regression case before activation."
        if report.score_b != 1.0 or report.score_a != sum(
            r["supported_a"] for r in report.results
        ) / len(report.results):
            return (
                "Evaluation scores are inconsistent with per-case results. Run a fresh evaluation."
            )
        return None

    def approve_version(
        self,
        skill_id: str,
        version: int,
        *,
        evaluation_id: str = "",
        confirmed: bool = False,
        reviewer: str = "",
        reason: str = "",
    ) -> Skill:
        """Activate only an explicitly human-reviewed candidate with passing stored evidence."""
        skill = self.store.get_skill(skill_id)
        sv = self.store.get_skill_version(skill_id, version)
        self._confirmation(confirmed, reviewer, reason)
        if not evaluation_id:
            raise ApprovalRequiredError(
                "Provide a saved evaluation_id after reviewing its per-case results."
            )
        report = self.store.get_eval_report(evaluation_id)
        issue = self._report_issue(skill, sv, report)
        if issue:
            raise ApprovalRequiredError(issue)
        ensure_no_secrets(json.dumps(sv.to_dict(), ensure_ascii=False), where="candidate")
        return self.store.activate_version(
            skill_id,
            version,
            expected_active=skill.active_version,
            expected_status="candidate",
            event="approve",
            evaluation_id=evaluation_id,
            reviewer=reviewer.strip(),
            reason=reason.strip(),
        )

    @staticmethod
    def _confirmation(confirmed: bool, reviewer: str, reason: str) -> None:
        if (
            confirmed is not True
            or not isinstance(reviewer, str)
            or not reviewer.strip()
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ApprovalRequiredError(
                "Review the candidate and evaluation with the user, then supply confirmed=true, "
                "reviewer and reason. A model must not grant its own approval."
            )
        ensure_no_secrets(reviewer + "\n" + reason, where="review metadata")

    def rollback_to(
        self,
        skill_id: str,
        version: int,
        *,
        confirmed: bool = False,
        reviewer: str = "",
        reason: str = "",
    ) -> Skill:
        """Move the active pointer back to a previously approved version.

        The abandoned active version is marked ``rolled_back`` (auditable);
        its content is never modified.
        """
        skill = self.store.get_skill(skill_id)
        target = self.store.get_skill_version(skill_id, version)
        self._confirmation(confirmed, reviewer, reason)

        if target.status not in ("approved", "rolled_back"):
            raise VersionStateError(
                f"Cannot roll back to v{version}: its status is "
                f"'{target.status}'. Only previously human-approved versions "
                "can be rolled back to."
            )

        current = skill.active_version
        if current == version:
            return skill  # already active — nothing to do

        return self.store.activate_version(
            skill_id,
            version,
            expected_active=current,
            expected_status=target.status,
            event="rollback",
            evaluation_id=None,
            reviewer=reviewer.strip(),
            reason=reason.strip(),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _actual_diff(before: SkillVersion | None, after: SkillVersion) -> list[dict[str, Any]]:
    previous = before.to_dict() if before else {}
    current = after.to_dict()
    fields = (
        "summary",
        "trigger_conditions",
        "decision_rules",
        "exception_handling",
        "anti_patterns",
        "open_questions",
        "derived_from",
    )
    return [
        {"field": key, "before": previous.get(key), "after": current[key]}
        for key in fields
        if previous.get(key) != current[key]
    ]


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
