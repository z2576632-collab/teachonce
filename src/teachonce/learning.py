"""Learning engine — demonstrations in, candidate skills out.

This is TeachOnce's core IP (see docs/PRODUCT.md §4.1): infer *transferable
decision knowledge* from multiple demonstrations, not transcriptions of
operations. The LLM does inference; this module owns schema validation,
evidence grounding, and persistence discipline.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .errors import LLMNotConfiguredError, SchemaValidationError
from .llm import LLMProvider, parse_json_response
from .models import DecisionRule, Demonstration, Skill, SkillVersion
from .security import ensure_no_secrets
from .store import SkillStore

_LEARN_SYSTEM_PROMPT = """\
You are TeachOnce's skill-extraction engine.

You receive one or more structured DEMONSTRATIONS of how a user completed a
task. Your job is NOT to transcribe operations. Infer the user's transferable
working knowledge:

1. TRIGGER CONDITIONS — when does this skill apply at all?
2. DECISION RULES — "under condition C, the user does D, because R".
   Generalize across demonstrations: prefer rules that explain the user's
   choices in MULTIPLE observations, or that capture a genuine conditional
   difference (when X -> A, when Y -> B). Every rule MUST list the exact
   demonstration ids that support it in "sources".
3. EXCEPTION HANDLING — edge cases, ambiguity, and error situations the
   user handled, and how.
4. ANTI-PATTERNS — approaches the evidence shows the user avoids.
5. OPEN QUESTIONS — what the demonstrations do NOT cover. NEVER invent
   facts, preferences, or rationale that is not grounded in the provided
   materials; when evidence is thin, put the uncertainty in open_questions.

Write all output text in the SAME LANGUAGE as the demonstrations.
Respond with a single JSON object (no markdown fences):

{
  "summary": "<one sentence: what work this skill covers>",
  "trigger_conditions": ["<when to use this skill>", "..."],
  "decision_rules": [
    {"condition": "...", "decision": "...", "rationale": "...",
     "confidence": 0.0, "sources": ["<demo id>", "..."]}
  ],
  "exception_handling": [{"situation": "...", "handling": "..."}],
  "anti_patterns": ["..."],
  "open_questions": ["..."]
}
"""


class LearningEngine:
    """Imports demonstrations and extracts candidate skills from them.

    ``llm`` is optional: importing demonstrations is a local-only
    operation. Extraction requires an LLM and raises
    :class:`LLMNotConfiguredError` when none is available.
    """

    def __init__(self, store: SkillStore, llm: LLMProvider | None = None) -> None:
        self.store = store
        self.llm = llm

    def _require_llm(self) -> LLMProvider:
        if self.llm is None:
            raise LLMNotConfiguredError(
                "Skill extraction requires an LLM. Configure "
                "TEACHCONE_LLM_API_KEY (optionally TEACHCONE_LLM_BASE_URL / "
                "TEACHCONE_LLM_MODEL), then retry."
            )
        return self.llm

    # ------------------------------------------------------------------
    # step 1: import a demonstration
    # ------------------------------------------------------------------

    def import_demonstration(self, payload: dict[str, Any]) -> Demonstration:
        """Validate, secret-scan, and store one structured demonstration.

        ``payload`` keys: ``task_goal`` (required), ``steps``, ``context``,
        ``key_judgments``, ``outcome``, ``tags``.
        """
        if not isinstance(payload, dict):
            raise SchemaValidationError("Demonstration must be a JSON object.")
        ensure_no_secrets(json.dumps(payload, ensure_ascii=False), where="demonstration")
        task_goal = _text(payload.get("task_goal", ""), "task_goal")
        if not task_goal:
            raise SchemaValidationError(
                "Demonstration requires a non-empty 'task_goal' — state what "
                "the user was trying to accomplish."
            )

        steps = payload.get("steps", [])
        if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
            raise SchemaValidationError("Demonstration steps must be a list of objects.")
        for step in steps:
            if not _text(step.get("action", ""), "action"):
                raise SchemaValidationError("Each demonstration step needs a non-empty action.")
            for field in ("target", "decision", "rationale"):
                _text(step.get(field, ""), field)
        demo = Demonstration.create(
            task_goal=task_goal,
            steps=steps,
            context=_text(payload.get("context", ""), "context"),
            key_judgments=_as_judgments(payload.get("key_judgments")),
            outcome=_text(payload.get("outcome", ""), "outcome"),
            tags=_clean_str_list(payload.get("tags", [])),
        )

        # Safety red line: never store credentials (AGENTS.md §5.1).
        ensure_no_secrets(json.dumps(demo.to_dict(), ensure_ascii=False), where="demonstration")

        if not demo.steps and not demo.key_judgments:
            raise SchemaValidationError(
                "Demonstration must contain at least one step or key judgment "
                "— otherwise there is nothing to learn from."
            )

        self.store.add_demonstration(demo)
        return demo

    # ------------------------------------------------------------------
    # step 2: extract a candidate skill from demonstrations
    # ------------------------------------------------------------------

    def learning_context(self, name: str, demonstration_ids: list[str]) -> dict[str, Any]:
        """Return real evidence and extraction instructions for an external host model."""
        if not isinstance(name, str) or not name.strip() or not demonstration_ids:
            raise SchemaValidationError("Provide a skill name and demonstration ids.")
        if len(set(demonstration_ids)) != len(demonstration_ids):
            raise SchemaValidationError("Demonstration ids must be distinct.")
        demos = [self.store.get_demonstration(did) for did in demonstration_ids]
        prompt = _format_demonstrations(name, demos)
        ensure_no_secrets(prompt, where="learning context")
        return {
            "system_prompt": _LEARN_SYSTEM_PROMPT,
            "user_prompt": prompt,
            "demonstration_ids": demonstration_ids,
            "server_inference_performed": False,
        }

    def extract_candidate_skill(
        self,
        name: str,
        demonstration_ids: list[str],
        *,
        description: str = "",
        proposal: dict[str, Any] | None = None,
    ) -> tuple[Skill, SkillVersion]:
        """Synthesize a v1 candidate skill from one or more demonstrations."""
        if not name.strip():
            raise SchemaValidationError("Skill name must not be empty.")
        if not demonstration_ids:
            raise SchemaValidationError("Provide at least one demonstration id to learn from.")
        if len(set(demonstration_ids)) != len(demonstration_ids):
            raise SchemaValidationError("Demonstration ids must be distinct.")
        ensure_no_secrets(name + "\n" + description, where="skill metadata")

        demos = [self.store.get_demonstration(did) for did in demonstration_ids]
        user_prompt = _format_demonstrations(name, demos)

        if proposal is None:
            llm = self._require_llm()
            raw = llm.complete(_LEARN_SYSTEM_PROMPT, user_prompt, json_mode=True)
        else:
            # Explicit caller input, never an automatic fallback or fake model call.
            raw = json.dumps(proposal, ensure_ascii=False)
        data = parse_json_response(raw)
        cleaned = validate_skill_payload(data, valid_ids={d.id for d in demos})

        skill = Skill.create(
            name=name.strip(),
            description=description.strip() or cleaned["summary"],
        )
        sv = SkillVersion(
            skill_id=skill.id,
            version=1,
            trigger_conditions=cleaned["trigger_conditions"],
            decision_rules=cleaned["decision_rules"],
            exception_handling=cleaned["exception_handling"],
            anti_patterns=cleaned["anti_patterns"],
            open_questions=cleaned["open_questions"],
            summary=cleaned["summary"],
            changelog=(f"Initial version: extracted from {len(demos)} demonstration(s)."),
            status="candidate",
            derived_from={
                "demonstrations": [d.id for d in demos],
                **({"inference_sources": ["host-proposal"]} if proposal is not None else {}),
            },
        )
        self.store.create_skill_with_version(skill, sv)
        return skill, sv


# ---------------------------------------------------------------------------
# prompt formatting
# ---------------------------------------------------------------------------


def _format_demonstrations(name: str, demos: list[Demonstration]) -> str:
    lines: list[str] = [f"# Skill name\n{name}", f"\n# Demonstrations ({len(demos)})"]
    for demo in demos:
        lines.append(f"\n## demonstration id={demo.id}")
        lines.append(f"Goal: {demo.task_goal}")
        if demo.context:
            lines.append(f"Context: {demo.context}")
        if demo.steps:
            lines.append("Steps:")
            for idx, step in enumerate(demo.steps, start=1):
                parts = [f"{idx}. action: {step.action}"]
                if step.target:
                    parts.append(f"target: {step.target}")
                if step.decision:
                    parts.append(f"decision: {step.decision}")
                if step.rationale:
                    parts.append(f"rationale: {step.rationale}")
                lines.append("   " + " | ".join(parts))
        if demo.key_judgments:
            lines.append("Key judgments:")
            for j in demo.key_judgments:
                lines.append(
                    f"   - situation: {j.get('situation', '')} | "
                    f"decision: {j.get('decision', '')} | "
                    f"why: {j.get('why', '')}"
                )
        if demo.outcome:
            lines.append(f"Outcome: {demo.outcome}")
        if demo.tags:
            lines.append(f"Tags: {', '.join(demo.tags)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _as_judgments(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise SchemaValidationError("'key_judgments' must be a list.")
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SchemaValidationError(
                "Each key judgment must be an object with situation/decision/why."
            )
        out.append({str(k): _text(v, "key judgment") for k, v in item.items()})
    return out


def validate_skill_payload(data: dict[str, Any], *, valid_ids: set[str]) -> dict[str, Any]:
    """Validate an LLM skill payload; never trust it as-is.

    Shared by ``learning.py`` (extraction) and ``correction.py`` (revision).

    - Required: >=1 trigger condition, >=1 decision rule with non-empty
      condition and decision.
    - Evidence groundings in ``sources`` are filtered to ids that actually
      exist (hallucinated references are dropped and recorded).
    - Malformed list items are dropped instead of silently mis-stored.
    """
    if not isinstance(data, dict):
        raise SchemaValidationError("Skill payload must be a JSON object.")
    ensure_no_secrets(json.dumps(data, ensure_ascii=False), where="skill payload")
    summary = _text(data.get("summary", ""), "summary")

    trigger_conditions = _clean_str_list(data.get("trigger_conditions"))
    if not trigger_conditions:
        raise SchemaValidationError(
            "Extraction produced no trigger conditions — cannot build a "
            "usable skill. Check that the demonstrations describe a task."
        )

    rules: list[DecisionRule] = []
    rule_ids: set[str] = set()
    dropped_sources: list[str] = []
    raw_rules = data.get("decision_rules", [])
    if not isinstance(raw_rules, list):
        raise SchemaValidationError("decision_rules must be a list of objects.")
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        condition = _text(item.get("condition", ""), "condition")
        decision = _text(item.get("decision", ""), "decision")
        if not condition or not decision:
            continue
        rule_id = item.get("id")
        if rule_id is not None:
            if not isinstance(rule_id, str) or not rule_id.strip():
                raise SchemaValidationError("Decision rule 'id' must be a non-empty string.")
            rule_id = rule_id.strip()
            if rule_id in rule_ids:
                raise SchemaValidationError(
                    f"Duplicate decision rule id '{rule_id}'. Use a unique id for each rule."
                )
            rule_ids.add(rule_id)
        sources_raw = _clean_str_list(item.get("sources", []))
        sources = [s for s in sources_raw if s in valid_ids]
        dropped_sources.extend(s for s in sources_raw if s not in valid_ids)
        confidence = item.get("confidence", 0.5)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise SchemaValidationError("Rule confidence must be a finite JSON number from 0 to 1.")
        rules.append(
            DecisionRule.create(
                rule_id=rule_id,
                condition=condition,
                decision=decision,
                rationale=_text(item.get("rationale", ""), "rationale"),
                confidence=confidence,
                sources=sources,
            )
        )
    if not rules:
        raise SchemaValidationError(
            "Extraction produced no valid decision rules (each rule needs a "
            "non-empty condition and decision)."
        )

    exception_handling: list[dict[str, str]] = []
    raw_exceptions = data.get("exception_handling", [])
    if not isinstance(raw_exceptions, list):
        raise SchemaValidationError("exception_handling must be a list of objects.")
    for item in raw_exceptions:
        if not isinstance(item, dict):
            continue
        situation = _text(item.get("situation", ""), "situation")
        handling = _text(item.get("handling", ""), "handling")
        if situation and handling:
            exception_handling.append({"situation": situation, "handling": handling})

    open_questions = _clean_str_list(data.get("open_questions"))
    if dropped_sources:
        open_questions.append(
            "Sources referenced but not found among the demonstrations "
            f"(dropped as unverifiable): {', '.join(sorted(set(dropped_sources)))}"
        )

    return {
        "summary": summary,
        "trigger_conditions": trigger_conditions,
        "decision_rules": rules,
        "exception_handling": exception_handling,
        "anti_patterns": _clean_str_list(data.get("anti_patterns")),
        "open_questions": open_questions,
    }


def _clean_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise SchemaValidationError("Expected a list of strings, not coerced values.")
    return [x.strip() for x in raw if x.strip()]


def _text(raw: Any, field: str) -> str:
    if not isinstance(raw, str):
        raise SchemaValidationError(f"'{field}' must be text, not a coerced value.")
    return raw.strip()
