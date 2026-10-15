"""GEPA adapter — reflective evolution of a skill's decision rules.

GEPA (https://github.com/gepa-ai/gepa, MIT, UC Berkeley Sky Computing Lab)
is an LLM-based text evolution engine: it reads *why* candidates fail
(evaluation feedback), proposes targeted mutations, and keeps Pareto-best
variants. TeachOnce uses it to optimize the decision rules of an existing
skill version against tuning cases — the "self-evolution" step of the
learning loop (docs/PRODUCT.md §4.3).

Safety invariants preserved here (non-negotiable, see AGENTS.md §5):
- The optimized rules become a NEW **candidate** version. Nothing is
  auto-approved; the human approval gate in ``correction.py`` still decides
  whether the version may become active.
- History stays append-only; the base version is never mutated.
- The optimization budget (``max_metric_calls``) is explicit, and the run
  is recorded in the changelog so the origin of the candidate is auditable.
- ``gepa`` is an optional dependency (``pip install "teachonce[gepa]"``);
  it is imported lazily and never required by the core library or tests.

The judge that scores candidates is TeachOnce's own LLM judge
(``evaluation.py``), so GEPA optimizes for exactly the behavior our
evaluation engine measures — and its known limitation applies: the judge
sees the expected decision, so scores measure rule *support*, not
independent execution success.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import (
    AdapterUnavailableError,
    LLMNotConfiguredError,
    SchemaValidationError,
)
from ..evaluation import (
    _JUDGE_SYSTEM_PROMPT,
    _format_judge_prompt,
    _parse_judge_verdict,
    _validate_cases,
)
from ..learning import validate_skill_payload
from ..llm import LLMProvider, parse_json_response
from ..models import SkillVersion
from ..store import SkillStore

OptimizeFn = Callable[..., Any]
"""Injectable optimization callable (tests substitute a fake; production
defaults to the real ``gepa.optimize_anything``)."""

_RESTRUCTURE_SYSTEM_PROMPT = """\
You are TeachOnce's skill restructuring engine.

Input: an optimized RULES TEXT produced by an evolutionary optimizer,
plus the JSON of the ORIGINAL skill version it was derived from.

Re-express the optimized rules in the ORIGINAL skill's structured schema.
Hard rules:
- Preserve the meaning of the optimized text faithfully; do not add facts,
  preferences, or rationale that are not present in it.
- Keep rule "id"s of original rules that survive; use "rule_new_1" etc.
  for genuinely new ones.
- Write in the same language as the optimized text.
- "sources" must only reference ids from the original version's evidence.

Respond with a single JSON object (no markdown fences) with fields:
summary, trigger_conditions, decision_rules, exception_handling,
anti_patterns, open_questions
"""

_OBJECTIVE = (
    "Improve these personal-workflow decision rules so that, for each given "
    "situation, applying the rules yields the expected decision. Fix the "
    "failure modes described in the evaluation feedback without losing "
    "rules that already work."
)


@dataclass
class OptimizationOutcome:
    """Result of a GEPA run; best_score is a tuning score, not held-out reliability."""

    new_version: SkillVersion
    base_version: int
    best_score: float | None
    metric_calls_budget: int


def gepa_available() -> bool:
    """Whether the optional ``gepa`` package can be imported."""
    try:
        import gepa  # noqa: F401
    except ImportError:
        return False
    return True


def _require_gepa() -> None:
    if not gepa_available():
        raise AdapterUnavailableError(
            "GEPA optimization requires the optional 'gepa' package "
            "(MIT, https://github.com/gepa-ai/gepa). Install it with: "
            'pip install "teachonce[gepa]" — then retry.'
        )


def optimize_skill_rules(
    store: SkillStore,
    llm: LLMProvider | None,
    skill_id: str,
    cases: list[dict[str, Any]],
    *,
    base_version: int | None = None,
    max_metric_calls: int = 30,
    optimize_fn: OptimizeFn | None = None,
) -> OptimizationOutcome:
    """Evolve a skill version's rules with GEPA against tuning cases.

    ``cases`` become learning input once used by the optimizer. Reserve
    separate, independent cases for ``evaluation.evaluate``; normalized
    situation fingerprints persist with this candidate and its descendants.

    The same ``llm`` judges candidates AND powers GEPA's reflective
    mutation (wrapped as GEPA's ``LanguageModel`` protocol — no litellm or
    separate reflection model needed).

    ``optimize_fn`` is a dependency-injection seam for tests; production
    callers leave it ``None`` and the real ``gepa.optimize_anything`` runs.
    """
    if llm is None:
        raise LLMNotConfiguredError(
            "GEPA optimization needs an LLM for judging candidates and for "
            "restructuring the result. Configure TEACHCONE_LLM_API_KEY, "
            "then retry."
        )
    case_fingerprints = _validate_cases(cases, purpose="Optimization")
    if type(max_metric_calls) is not int or max_metric_calls < 1:
        raise SchemaValidationError("max_metric_calls must be a positive integer.")

    skill = store.get_skill(skill_id)
    if base_version is None:
        base_version = skill.active_version
    if base_version is None:
        versions = store.list_skill_versions(skill_id)
        if not versions:
            raise SchemaValidationError(f"Skill '{skill.name}' has no versions to optimize.")
        base_version = versions[-1].version
    base_sv = store.get_skill_version(skill_id, base_version)

    seed_rules = "\n".join(base_sv.rule_texts())
    if not seed_rules.strip():
        raise SchemaValidationError(
            f"Version v{base_version} has no rule text to optimize. "
            "Extract or revise the skill first."
        )

    def evaluator(candidate: str, example: dict[str, Any]) -> tuple[float, dict[str, str]]:
        score, feedback = _judge_rules_text(llm, candidate, example)
        return score, {"feedback": feedback}

    if optimize_fn is None:
        optimize_fn = _default_optimize_fn(llm)

    result = optimize_fn(
        seed_candidate=seed_rules,
        evaluator=evaluator,
        dataset=[
            {"situation": c["situation"], "expected_decision": c["expected_decision"]}
            for c in cases
        ],
        objective=_OBJECTIVE,
        max_metric_calls=max_metric_calls,
    )
    best_text, best_score = _extract_best(result)

    new_sv = _restructure(
        llm,
        store,
        base_sv,
        best_text,
        budget=max_metric_calls,
        case_fingerprints=case_fingerprints,
        activation_base=list(
            base_sv.derived_from.get("activation_base", [str(skill.active_version)])
        )
        if base_sv.status == "candidate"
        else [str(base_sv.version if skill.active_version is not None else None)],
    )

    return OptimizationOutcome(
        new_version=new_sv,
        base_version=base_version,
        best_score=best_score,
        metric_calls_budget=max_metric_calls,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _judge_rules_text(llm: LLMProvider, rules_text: str, case: dict[str, Any]) -> tuple[float, str]:
    """Score candidate rules text on one case with TeachOnce's LLM judge."""
    surrogate = SkillVersion(
        skill_id="gepa_candidate",
        version=0,
        trigger_conditions=[rules_text],
    )
    raw = llm.complete(_JUDGE_SYSTEM_PROMPT, _format_judge_prompt(surrogate, case), json_mode=True)
    supported, predicted, reasoning = _parse_judge_verdict(raw)
    feedback = f"predicted: {predicted} | {reasoning}"
    return (1.0 if supported else 0.0), feedback


def _default_optimize_fn(llm: LLMProvider) -> OptimizeFn:
    """Build the production optimize callable around gepa.optimize_anything.

    The reflection model is our own provider wrapped as GEPA's
    ``LanguageModel`` protocol (``prompt -> str``), so optimization uses
    whatever OpenAI-compatible endpoint the user configured — no litellm.
    """
    _require_gepa()
    from gepa.optimize_anything import (
        EngineConfig,
        GEPAConfig,
        ReflectionConfig,
        optimize_anything,
    )

    def run(**kwargs: Any) -> Any:
        budget = kwargs.pop("max_metric_calls")
        config = GEPAConfig(
            engine=EngineConfig(max_metric_calls=budget),
            reflection=ReflectionConfig(reflection_lm=_ReflectionLM(llm)),
        )
        return optimize_anything(config=config, **kwargs)

    return run


class _ReflectionLM:
    """Adapt ``LLMProvider.complete`` to GEPA's ``LanguageModel`` protocol."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    def __call__(self, prompt: str | list[dict[str, Any]]) -> str:
        system, user = _split_prompt(prompt)
        return self._llm.complete(system, user)


def _split_prompt(prompt: str | list[dict[str, Any]]) -> tuple[str, str]:
    if isinstance(prompt, str):
        return prompt, ""
    system_parts = []
    user_parts = []
    for message in prompt if isinstance(prompt, list) else []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        content = str(message.get("content", ""))
        (user_parts if role == "user" else system_parts).append(content)
    return "\n".join(system_parts), "\n".join(user_parts)


def _extract_best(result: Any) -> tuple[str, float | None]:
    """Pull the best candidate text (and score, if exposed) from a GEPAResult."""
    candidate = getattr(result, "best_candidate", None)
    if candidate is None and isinstance(result, dict):
        candidate = result.get("best_candidate")
    if isinstance(candidate, dict):
        # optimize_anything with a str seed returns a str; tolerate dicts.
        text = "\n".join(str(v) for v in candidate.values())
    elif candidate is not None:
        text = str(candidate)
    else:
        raise SchemaValidationError(
            "Optimizer returned no best candidate. Check the optimization "
            "budget and logs, then retry."
        )
    if not text.strip():
        raise SchemaValidationError("Optimizer produced an empty candidate; refusing to save it.")
    score = _best_score(result)
    return text, score


def _best_score(result: Any) -> float | None:
    """Best validation score, if the optimizer result exposes one.

    GEPAResult keeps scores as ``val_aggregate_scores[best_idx]``; fake
    results may expose ``best_score`` directly. ``None`` means unknown —
    the score is reported, never fabricated.
    """
    score = getattr(result, "best_score", None)
    if isinstance(score, (int, float)):
        return float(score)
    idx = getattr(result, "best_idx", None)
    scores = getattr(result, "val_aggregate_scores", None)
    if (
        isinstance(idx, int)
        and isinstance(scores, (list, tuple))
        and 0 <= idx < len(scores)
        and isinstance(scores[idx], (int, float))
    ):
        return float(scores[idx])
    return None


def _restructure(
    llm: LLMProvider,
    store: SkillStore,
    base_sv: SkillVersion,
    optimized_text: str,
    *,
    budget: int,
    case_fingerprints: list[str],
    activation_base: list[str],
) -> SkillVersion:
    """Turn optimized rules text into a structured candidate version."""
    user_prompt = (
        f"# Optimized rules text\n{optimized_text}\n\n"
        f"# Original skill version (v{base_sv.version}) as JSON\n"
        + json.dumps(base_sv.to_dict(), ensure_ascii=False, indent=2)
    )
    raw = llm.complete(_RESTRUCTURE_SYSTEM_PROMPT, user_prompt, json_mode=True)
    data = parse_json_response(raw)

    valid_ids = set(base_sv.derived_from.get("demonstrations", [])) | set(
        base_sv.derived_from.get("corrections", [])
    )
    cleaned = validate_skill_payload(data, valid_ids=valid_ids)

    sv = SkillVersion(
        skill_id=base_sv.skill_id,
        version=0,  # Allocated by save_candidate atomically after the model returns.
        trigger_conditions=cleaned["trigger_conditions"],
        decision_rules=cleaned["decision_rules"],
        exception_handling=cleaned["exception_handling"],
        anti_patterns=cleaned["anti_patterns"],
        open_questions=cleaned["open_questions"],
        summary=cleaned["summary"],
        changelog=(
            f"GEPA reflective optimization of v{base_sv.version} "
            f"(budget: {budget} metric calls). Rules were evolved against "
            "tuning cases and restructured into the skill schema. "
            "Candidate — requires human approval and regression evaluation "
            "before becoming active."
        ),
        changes=[
            {
                "op": "modify",
                "rule_id": "",
                "description": "Rules evolved by GEPA optimization; review "
                "the full diff against the base version before approving.",
            }
        ],
        status="candidate",
        derived_from={
            **{key: list(values) for key, values in base_sv.derived_from.items()},
            "optimizations": [
                *base_sv.derived_from.get("optimizations", []),
                f"gepa:v{base_sv.version}:budget{budget}",
            ],
            "optimization_cases": sorted(
                set(base_sv.derived_from.get("optimization_cases", [])) | set(case_fingerprints)
            ),
            "base_versions": [str(base_sv.version)],
            "activation_base": activation_base,
        },
    )
    return store.save_candidate(sv)
