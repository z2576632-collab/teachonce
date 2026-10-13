"""Core data models for TeachOnce.

All models are plain dataclasses with no IO side effects (see AGENTS.md).
Serialization boundary: ``to_dict`` / ``from_dict`` produce / consume
JSON-compatible structures (what MCP tools and the store use).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

SkillVersionStatus = Literal["candidate", "approved", "rolled_back"]
CorrectionType = Literal["wrong_decision", "missing_rule", "too_broad", "too_narrow", "other"]
CorrectionStatus = Literal["open", "applied"]


def utc_now() -> str:
    """Current UTC time as ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    """Short unique id, e.g. ``demo_3f9a2c81b0d4``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Demonstrations
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """A single observed step inside a demonstration."""

    action: str
    target: str = ""
    decision: str = ""
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Observation:
        return cls(
            action=str(data.get("action", "")),
            target=str(data.get("target", "")),
            decision=str(data.get("decision", "")),
            rationale=str(data.get("rationale", "")),
        )


@dataclass
class Demonstration:
    """One recorded example of a user completing a task."""

    id: str
    task_goal: str
    steps: list[Observation] = field(default_factory=list)
    context: str = ""
    key_judgments: list[dict[str, str]] = field(default_factory=list)
    outcome: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        task_goal: str,
        steps: list[dict[str, Any] | Observation] | None = None,
        *,
        context: str = "",
        key_judgments: list[dict[str, str]] | None = None,
        outcome: str = "",
        tags: list[str] | None = None,
    ) -> Demonstration:
        parsed_steps = [
            s if isinstance(s, Observation) else Observation.from_dict(s) for s in (steps or [])
        ]
        return cls(
            id=new_id("demo"),
            task_goal=task_goal,
            steps=parsed_steps,
            context=context,
            key_judgments=list(key_judgments or []),
            outcome=outcome,
            tags=list(tags or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_goal": self.task_goal,
            "steps": [s.to_dict() for s in self.steps],
            "context": self.context,
            "key_judgments": list(self.key_judgments),
            "outcome": self.outcome,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Demonstration:
        return cls(
            id=str(data.get("id") or new_id("demo")),
            task_goal=str(data.get("task_goal", "")),
            steps=[Observation.from_dict(s) for s in data.get("steps", [])],
            context=str(data.get("context", "")),
            key_judgments=[dict(j) for j in data.get("key_judgments", [])],
            outcome=str(data.get("outcome", "")),
            tags=[str(t) for t in data.get("tags", [])],
            created_at=str(data.get("created_at") or utc_now()),
        )


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@dataclass
class DecisionRule:
    """A transferable decision rule: condition -> decision, with rationale.

    This is the core learning product of TeachOnce: not "what was clicked",
    but "under which conditions to decide what, and why".
    """

    id: str
    condition: str
    decision: str
    rationale: str = ""
    confidence: float = 0.5
    sources: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        condition: str,
        decision: str,
        *,
        rationale: str = "",
        confidence: float = 0.5,
        sources: list[str] | None = None,
        rule_id: str | None = None,
    ) -> DecisionRule:
        return cls(
            id=rule_id or new_id("rule"),
            condition=condition,
            decision=decision,
            rationale=rationale,
            confidence=_clamp01(confidence),
            sources=list(sources or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionRule:
        return cls(
            id=str(data.get("id") or new_id("rule")),
            condition=str(data.get("condition", "")),
            decision=str(data.get("decision", "")),
            rationale=str(data.get("rationale", "")),
            confidence=_clamp01(float(data.get("confidence", 0.5))),
            sources=[str(s) for s in data.get("sources", [])],
        )


@dataclass
class SkillVersion:
    """One immutable revision of a skill.

    History is append-only: updates create a new version, never mutate
    an existing one. ``status`` moves candidate -> approved via explicit
    human approval. ``Skill.active_version`` points at the approved
    version the matcher may use.
    """

    skill_id: str
    version: int
    trigger_conditions: list[str] = field(default_factory=list)
    decision_rules: list[DecisionRule] = field(default_factory=list)
    exception_handling: list[dict[str, str]] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    changelog: str = ""
    changes: list[dict[str, str]] = field(default_factory=list)
    status: SkillVersionStatus = "candidate"
    derived_from: dict[str, list[str]] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    summary: str = ""
    created_at: str = field(default_factory=utc_now)

    def rule_texts(self) -> list[str]:
        """Flat text of all rules — used by heuristic evaluation."""
        chunks: list[str] = []
        chunks.extend(self.trigger_conditions)
        for r in self.decision_rules:
            chunks.append(f"{r.condition} -> {r.decision} ({r.rationale})")
        for e in self.exception_handling:
            chunks.append(f"{e.get('situation', '')} -> {e.get('handling', '')}")
        chunks.extend(self.anti_patterns)
        return [c for c in chunks if c.strip()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "trigger_conditions": list(self.trigger_conditions),
            "decision_rules": [r.to_dict() for r in self.decision_rules],
            "exception_handling": [dict(e) for e in self.exception_handling],
            "anti_patterns": list(self.anti_patterns),
            "changelog": self.changelog,
            "changes": [dict(c) for c in self.changes],
            "status": self.status,
            "derived_from": {k: list(v) for k, v in self.derived_from.items()},
            "open_questions": list(self.open_questions),
            "summary": self.summary,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillVersion:
        return cls(
            skill_id=str(data.get("skill_id", "")),
            version=int(data.get("version", 1)),
            trigger_conditions=[str(t) for t in data.get("trigger_conditions", [])],
            decision_rules=[DecisionRule.from_dict(r) for r in data.get("decision_rules", [])],
            exception_handling=[
                {str(k): str(v) for k, v in e.items()} for e in data.get("exception_handling", [])
            ],
            anti_patterns=[str(a) for a in data.get("anti_patterns", [])],
            changelog=str(data.get("changelog", "")),
            changes=[{str(k): str(v) for k, v in c.items()} for c in data.get("changes", [])],
            status=_valid_status(data.get("status", "candidate")),
            derived_from={
                str(k): [str(x) for x in v] for k, v in dict(data.get("derived_from", {})).items()
            },
            open_questions=[str(q) for q in data.get("open_questions", [])],
            summary=str(data.get("summary", "")),
            created_at=str(data.get("created_at") or utc_now()),
        )


@dataclass
class Skill:
    """A learned skill: stable identity + pointer to the active version."""

    id: str
    name: str
    description: str = ""
    active_version: int | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, name: str, description: str = "") -> Skill:
        return cls(id=new_id("skill"), name=name, description=description)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Skill:
        raw_active = data.get("active_version")
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            active_version=None if raw_active is None else int(raw_active),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )


# ---------------------------------------------------------------------------
# Corrections / evaluation / matching
# ---------------------------------------------------------------------------


@dataclass
class Correction:
    """A natural-language correction from the user about a skill version."""

    id: str
    skill_id: str
    target_version: int
    feedback: str
    correction_type: CorrectionType = "other"
    analysis: str = ""
    status: CorrectionStatus = "open"
    applied_in_version: int | None = None
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        skill_id: str,
        target_version: int,
        feedback: str,
        *,
        correction_type: CorrectionType = "other",
        analysis: str = "",
    ) -> Correction:
        return cls(
            id=new_id("corr"),
            skill_id=skill_id,
            target_version=target_version,
            feedback=feedback,
            correction_type=correction_type,
            analysis=analysis,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Correction:
        applied = data.get("applied_in_version")
        ctype = str(data.get("correction_type", "other"))
        valid_types = {"wrong_decision", "missing_rule", "too_broad", "too_narrow", "other"}
        return cls(
            id=str(data.get("id") or new_id("corr")),
            skill_id=str(data.get("skill_id", "")),
            target_version=int(data.get("target_version", 1)),
            feedback=str(data.get("feedback", "")),
            correction_type=ctype if ctype in valid_types else "other",  # type: ignore[arg-type]
            analysis=str(data.get("analysis", "")),
            status="applied" if str(data.get("status")) == "applied" else "open",
            applied_in_version=None if applied is None else int(applied),
            created_at=str(data.get("created_at") or utc_now()),
        )


@dataclass
class EvalReport:
    """Comparison of two skill versions on held-out cases.

    ``method`` records how verdicts were produced ("llm_judge" or
    "heuristic"). Reports must state method limitations — see
    ``evaluation.py`` docstrings for the exact caveats.
    """

    id: str
    skill_id: str
    version_a: int
    version_b: int
    method: str
    score_a: float
    score_b: float
    results: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        skill_id: str,
        version_a: int,
        version_b: int,
        method: str,
        score_a: float,
        score_b: float,
        *,
        results: list[dict[str, Any]] | None = None,
        notes: str = "",
    ) -> EvalReport:
        return cls(
            id=new_id("eval"),
            skill_id=skill_id,
            version_a=version_a,
            version_b=version_b,
            method=method,
            score_a=score_a,
            score_b=score_b,
            results=list(results or []),
            notes=notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatchResult:
    """Outcome of matching a task against learned skills.

    A match never executes anything: it selects a skill, explains why,
    and proposes a plan for the host agent. Low confidence or danger
    flags route to human confirmation instead of guessing.
    """

    task: str
    matched: bool
    skill_id: str | None = None
    skill_name: str | None = None
    version: int | None = None
    confidence: float = 0.0
    rationale: str = ""
    plan: list[str] = field(default_factory=list)
    requires_confirmation: bool = True
    missing_info: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _valid_status(value: object) -> SkillVersionStatus:
    text = str(value)
    if text in ("candidate", "approved", "rolled_back"):
        return text  # type: ignore[return-value]
    return "candidate"
