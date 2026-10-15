"""Evaluation engine — did the new version actually get better?

Compares two skill versions on held-out cases (never used for learning).
Honesty rules (AGENTS.md §5.6): the report records *how* verdicts were
produced and states the method's limitations.

Two methods:
- ``llm_judge``  — the judge simulates the decision each version's rules
  would produce and compares it with the expected decision. Judge bias is
  possible; individual verdicts are included for spot-checking.
- ``heuristic``  — verdict = do the rule texts contain the case's expected
  keywords (>= 50% coverage). Keyword overlap, NOT semantic understanding;
  a cheap offline regression signal, not a quality score.

Method ``auto`` picks ``llm_judge`` when an LLM is available, else
``heuristic``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from .errors import LLMNotConfiguredError, SchemaValidationError
from .llm import LLMProvider, parse_json_response
from .models import EvalReport, SkillVersion
from .security import ensure_no_secrets
from .store import SkillStore

_HEURISTIC_MIN_HIT_RATIO = 0.5

_OVERLAP_NOTES = (
    "Case isolation: normalized situation fingerprints were checked for duplicates "
    "and overlap with recorded GEPA optimization cases of both versions. This does "
    "not detect paraphrases or semantic overlap with demonstrations/corrections; "
    "older optimization versions may lack fingerprints. Independently prepare "
    "held-out cases."
)

_DEFAULT_NOTES = {
    "heuristic": (
        "Heuristic evaluation: verdict = the rule texts contain the case's "
        "expected keywords (>=50% coverage). Keyword overlap only — NOT "
        "semantic understanding. Use as a cheap regression signal, not a "
        "quality score."
    ),
    "llm_judge": (
        "LLM-judge evaluation: the judge simulates the decision each "
        "version's rules would produce for each held-out case and compares "
        "it with the expected decision. Judge bias is possible — spot-check "
        "individual verdicts in 'results'."
    ),
}

_JUDGE_SYSTEM_PROMPT = """\
You are a strict evaluator for a learned skill.

You receive one skill version's rules and one SITUATION. Simulate what a
careful assistant that knows ONLY these rules would decide in that situation.
Then judge whether the simulated decision matches the EXPECTED decision.

Be strict: if the rules do not clearly cover the situation, the simulated
decision should be "rules do not cover this" and matches_expected = false.

Write output text in the SAME LANGUAGE as the situation.
Respond with a single JSON object (no markdown fences):
{"predicted_decision": "...", "matches_expected": true|false, "reasoning": "..."}
"""


class SkillEvaluator:
    """Runs held-out-case comparisons between two versions of a skill."""

    def __init__(self, store: SkillStore, llm: LLMProvider | None = None) -> None:
        self.store = store
        self.llm = llm

    def evaluate(
        self,
        skill_id: str,
        version_a: int,
        version_b: int,
        cases: list[dict[str, Any]],
        *,
        method: str = "auto",
        notes: str = "",
    ) -> EvalReport:
        """Compare ``version_a`` vs ``version_b`` on ``cases``; store the report.

        ``cases``: ``[{"situation": ..., "expected_decision": ...,
        "keywords": [...] (optional, used by heuristic)}]``.
        """
        case_fingerprints = _validate_cases(cases, purpose="Evaluation")
        if not isinstance(notes, str):
            raise SchemaValidationError("Evaluation notes must be a string.")
        ensure_no_secrets(notes, where="evaluation notes")
        if method == "auto":
            method = "llm_judge" if self.llm is not None else "heuristic"
        if method not in ("llm_judge", "heuristic"):
            raise SchemaValidationError(
                f"Unknown evaluation method '{method}' — use 'llm_judge', 'heuristic', or 'auto'."
            )
        if method == "llm_judge" and self.llm is None:
            raise LLMNotConfiguredError(
                "llm_judge evaluation requires an LLM. Configure "
                "TEACHCONE_LLM_API_KEY or use method='heuristic'."
            )

        sv_a = self.store.get_skill_version(skill_id, version_a)
        sv_b = self.store.get_skill_version(skill_id, version_b)
        seen = set(sv_a.derived_from.get("optimization_cases", [])) | set(
            sv_b.derived_from.get("optimization_cases", [])
        )
        for idx, case_fingerprint in enumerate(case_fingerprints):
            if case_fingerprint in seen:
                raise SchemaValidationError(
                    f"Case #{idx} was used for optimization of a compared version. "
                    "Supply independent held-out situations, not the tuning cases."
                )

        results: list[dict[str, Any]] = []
        for idx, case in enumerate(cases):
            ok_a, detail_a = self._verdict(sv_a, case, method)
            ok_b, detail_b = self._verdict(sv_b, case, method)
            results.append(
                {
                    "case_index": idx,
                    "situation_fingerprint": case_fingerprints[idx],
                    "situation": str(case.get("situation", "")),
                    "expected_decision": str(case.get("expected_decision", "")),
                    "supported_a": ok_a,
                    "detail_a": detail_a,
                    "supported_b": ok_b,
                    "detail_b": detail_b,
                }
            )

        total = len(results)
        score_a = sum(1 for r in results if r["supported_a"]) / total
        score_b = sum(1 for r in results if r["supported_b"]) / total

        report = EvalReport.create(
            skill_id=skill_id,
            version_a=version_a,
            version_b=version_b,
            method=method,
            score_a=score_a,
            score_b=score_b,
            results=results,
            notes=_DEFAULT_NOTES[method]
            + "\n"
            + _OVERLAP_NOTES
            + (f"\nUser notes: {notes.strip()}" if notes.strip() else ""),
        )
        self.store.save_eval_report(report)
        return report

    # ------------------------------------------------------------------

    def _verdict(self, sv: SkillVersion, case: dict[str, Any], method: str) -> tuple[bool, str]:
        if method == "heuristic":
            return _heuristic_verdict(sv, case)
        return self._llm_verdict(sv, case)

    def _llm_verdict(self, sv: SkillVersion, case: dict[str, Any]) -> tuple[bool, str]:
        assert self.llm is not None  # guarded in evaluate()
        user_prompt = _format_judge_prompt(sv, case)
        raw = self.llm.complete(_JUDGE_SYSTEM_PROMPT, user_prompt, json_mode=True)
        supported, predicted, reasoning = _parse_judge_verdict(raw)
        detail = f"predicted: {predicted} | {reasoning}"
        return supported, detail


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _case_fingerprint(situation: str) -> str:
    """Versioned normalized identity, deliberately excluding the expected answer."""
    normalized = " ".join(unicodedata.normalize("NFKC", situation).casefold().split())
    return "situation-v1:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_cases(cases: list[dict[str, Any]], *, purpose: str) -> list[str]:
    """Validate the whole batch before any model call or optimization."""
    if not isinstance(cases, list) or not cases:
        raise SchemaValidationError(f"{purpose} needs at least one case.")
    for idx, case in enumerate(cases):
        _validate_case(case, idx)
    try:
        serialized = json.dumps(cases, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError("Cases must contain JSON-compatible values.") from exc
    ensure_no_secrets(serialized, where=f"{purpose.lower()} cases")
    fingerprints = [_case_fingerprint(case["situation"]) for case in cases]
    if len(set(fingerprints)) != len(fingerprints):
        raise SchemaValidationError(
            f"{purpose} cases must have distinct situations. Remove duplicate cases; "
            "changing an expected answer or case ID does not create a new situation."
        )
    return fingerprints


def _parse_judge_verdict(raw: str) -> tuple[bool, str, str]:
    """Use the same strict judge contract in evaluation and optimization."""
    ensure_no_secrets(raw, where="judge response")
    data = parse_json_response(raw)
    supported = data.get("matches_expected")
    if not isinstance(supported, bool):
        raise SchemaValidationError(
            "Evaluation 'matches_expected' must be a JSON boolean (true/false), "
            "not a string or number. Retry the evaluation."
        )
    for field in ("predicted_decision", "reasoning"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SchemaValidationError(f"Evaluation needs a non-empty '{field}' string.")
    return supported, data["predicted_decision"].strip(), data["reasoning"].strip()


def _validate_case(case: dict[str, Any], idx: int) -> None:
    if not isinstance(case, dict):
        raise SchemaValidationError(f"Case #{idx} must be an object.")
    for field in ("situation", "expected_decision"):
        value = case.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SchemaValidationError(f"Case #{idx} needs a non-empty '{field}' string.")
    keywords = case.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list)
        or any(not isinstance(word, str) or not word.strip() for word in keywords)
    ):
        raise SchemaValidationError(f"Case #{idx} 'keywords' must be a list of non-empty strings.")


def _heuristic_verdict(sv: SkillVersion, case: dict[str, Any]) -> tuple[bool, str]:
    keywords_raw = case.get("keywords")
    if isinstance(keywords_raw, list) and keywords_raw:
        keywords = [str(k).strip() for k in keywords_raw if str(k).strip()]
    else:
        keywords = [str(case.get("expected_decision", "")).strip()]
    if not keywords:
        return False, "no keywords available for heuristic verdict"

    blob = "\n".join(sv.rule_texts()).lower()
    hits = [k for k in keywords if k.lower() in blob]
    missed = [k for k in keywords if k.lower() not in blob]
    ratio = len(hits) / len(keywords)
    supported = ratio >= _HEURISTIC_MIN_HIT_RATIO
    detail = f"keyword hits {len(hits)}/{len(keywords)}"
    if missed:
        detail += f" (missed: {', '.join(missed)})"
    return supported, detail


def _format_judge_prompt(sv: SkillVersion, case: dict[str, Any]) -> str:
    lines = [f"# Skill rules (v{sv.version})"]
    if sv.summary:
        lines.append(f"summary: {sv.summary}")
    lines.append("trigger_conditions:")
    for cond in sv.trigger_conditions:
        lines.append(f"  - {cond}")
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
    lines.append(f"\n# Situation\n{case.get('situation', '')}")
    lines.append(f"\n# Expected decision\n{case.get('expected_decision', '')}")
    return "\n".join(lines)
