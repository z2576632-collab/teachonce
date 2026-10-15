"""Host execution reports are evidence, not proof of execution or automatic rule changes."""

from __future__ import annotations

import json
from typing import Any

from .errors import SchemaValidationError
from .models import Correction, new_id, utc_now
from .security import ensure_no_secrets
from .store import SkillStore


class FeedbackEngine:
    """Persist reported outcomes and explicit correction feedback without requiring a model."""

    def __init__(self, store: SkillStore) -> None:
        self.store = store

    def record_outcome(
        self,
        skill_id: str,
        version: int,
        task: str,
        status: str,
        summary: str,
        *,
        feedback: str = "",
    ) -> dict[str, Any]:
        """Record a host-reported result; explicit feedback becomes open, unapplied correction."""
        if status not in ("succeeded", "failed", "partial", "cancelled"):
            raise SchemaValidationError(
                "Outcome status must be succeeded, failed, partial or cancelled."
            )
        for field, value in {"task": task, "summary": summary, "feedback": feedback}.items():
            if not isinstance(value, str) or (field != "feedback" and not value.strip()):
                raise SchemaValidationError(
                    f"Outcome {field} must be {'non-empty ' if field != 'feedback' else ''}text."
                )
        self.store.get_skill_version(skill_id, version)
        outcome = {
            "id": new_id("outcome"),
            "skill_id": skill_id,
            "version": version,
            "task": task.strip(),
            "status": status,
            "summary": summary.strip(),
            "feedback": feedback.strip(),
            "created_at": utc_now(),
            "provenance": "host-reported; not independently verified",
        }
        ensure_no_secrets(json.dumps(outcome, ensure_ascii=False), where="execution outcome")
        correction = (
            Correction.create(
                skill_id,
                version,
                feedback.strip(),
                analysis=f"Explicit feedback associated with host-reported outcome {outcome['id']}.",
            )
            if feedback.strip()
            else None
        )
        outcome["correction_id"] = correction.id if correction else None
        self.store.save_outcome(outcome, correction)
        return outcome
