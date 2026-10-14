"""OpenAdapt adapter — GUI recording exports become Demonstrations.

OpenAdapt (https://github.com/OpenAdaptAI, MIT) records native desktop
demonstrations (screen + input + window evidence) via ``openadapt-capture``.
This adapter converts a recording **export** (JSON) into a TeachOnce
Demonstration, which then flows through the normal learning pipeline —
including the secrets scan in ``learning.import_demonstration``.

Safety red lines (AGENTS.md §5.3, docs/PRODUCT.md §7):
- GUI observation is **off by default**. Every call requires the explicit
  opt-in environment switch ``TEACHCONE_GUI_OBSERVATION=1``; otherwise a
  :class:`ConsentRequiredError` is raised. This is checked at call time so
  a recording can never be imported accidentally.
- This adapter only reads an export file the user already has. It does NOT
  start a recorder, hook input devices, or capture the screen itself —
  live capture stays with the OpenAdapt CLI, under the user's control.
- Screen recordings can contain anything on screen. The recording is
  scanned for secrets at import like any demonstration, but the user is
  responsible for reviewing the export first.

Supported input: the JSON export of an ``openadapt-capture`` recording —
an object with an action/event list. Field mapping is tolerant
("events"/"actions"/"recording"; per-event "type"/"action", "target"/
"window"/"element", "value"/"text") because OpenAdapt's export schema has
varied across releases; unmapped events are skipped and counted in the
returned report so nothing is silently dropped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..errors import ConsentRequiredError, SchemaValidationError
from ..learning import LearningEngine
from ..models import Demonstration
from ..store import SkillStore

CONSENT_ENV = "TEACHCONE_GUI_OBSERVATION"
_CONSENT_VALUES = {"1", "true", "yes", "on"}

# Action types that carry no learning signal for TeachOnce.
_IGNORED_EVENT_TYPES = {"mouse_move", "mousemove", "move", "scroll", "hover"}


def gui_observation_consented(env: dict[str, str] | None = None) -> bool:
    """Whether the user has explicitly opted in to GUI observation."""
    source = env if env is not None else os.environ
    return source.get(CONSENT_ENV, "").strip().lower() in _CONSENT_VALUES


def require_gui_consent(env: dict[str, str] | None = None) -> None:
    """Raise unless GUI observation was explicitly enabled by the user."""
    if not gui_observation_consented(env):
        raise ConsentRequiredError(
            "GUI observation is off by default. To import an OpenAdapt "
            f"recording, review the export for sensitive content, then set "
            f"{CONSENT_ENV}=1 in the environment and retry. TeachOnce never "
            "records or ingests screen data without this opt-in."
        )


def import_gui_recording(
    store: SkillStore,
    recording: str | Path | dict[str, Any],
    *,
    task_goal: str = "",
    context: str = "",
    outcome: str = "",
    tags: list[str] | None = None,
) -> Demonstration:
    """Convert an OpenAdapt recording export into a stored Demonstration.

    ``recording``: path to a JSON export, or the already-parsed dict.
    ``task_goal``: what the user was accomplishing (falls back to the
    recording's name/description metadata).
    """
    require_gui_consent()

    data = _load_recording(recording)
    steps, skipped = _events_to_steps(_event_list(data))
    if not steps:
        raise SchemaValidationError(
            "Recording contains no importable action events "
            f"({skipped} low-signal events skipped). Record a task with "
            "meaningful actions, or import a manual demonstration instead."
        )

    goal = task_goal.strip() or _recording_title(data)
    if not goal:
        raise SchemaValidationError(
            "Provide 'task_goal' — what the user was trying to accomplish. "
            "The recording has no usable name/description metadata."
        )

    engine = LearningEngine(store)  # local-only: import needs no LLM
    return engine.import_demonstration(
        {
            "task_goal": goal,
            "steps": steps,
            "context": context
            or f"Imported from an OpenAdapt GUI recording export ({skipped} low-signal events skipped).",
            "outcome": outcome,
            "tags": [*(tags or []), "gui-recording", "openadapt"],
        }
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _load_recording(recording: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(recording, dict):
        return recording
    path = Path(recording)
    if not path.is_file():
        raise SchemaValidationError(f"Recording export not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(
            f"Recording export is not valid JSON: {exc}. Export the recording "
            "from openadapt-capture as JSON and retry."
        ) from exc
    if not isinstance(data, dict):
        raise SchemaValidationError("Recording export must be a JSON object.")
    return data


def _event_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("events", "actions", "recording"):
        value = data.get(key)
        if isinstance(value, list):
            return [e for e in value if isinstance(e, dict)]
    # Some exports nest events under a "data"/"payload" object.
    for key in ("data", "payload"):
        nested = data.get(key)
        if isinstance(nested, dict):
            return _event_list(nested)
    raise SchemaValidationError(
        "Recording export has no 'events'/'actions' list. Expected an "
        "openadapt-capture JSON export; check the export format version."
    )


def _events_to_steps(events: list[dict[str, Any]]) -> tuple[list[dict[str, str]], int]:
    steps: list[dict[str, str]] = []
    skipped = 0
    for event in events:
        etype = str(event.get("type") or event.get("action") or event.get("name") or "").strip()
        if not etype or etype.lower() in _IGNORED_EVENT_TYPES:
            skipped += 1
            continue
        target = str(
            event.get("target") or event.get("window") or event.get("element") or ""
        ).strip()
        value = str(event.get("value") or event.get("text") or "").strip()
        action = etype if not value else f"{etype}: {value}"
        steps.append(
            {
                "action": action,
                "target": target,
                "decision": str(event.get("decision", "")).strip(),
                "rationale": str(event.get("rationale", "")).strip(),
            }
        )
    return steps, skipped


def _recording_title(data: dict[str, Any]) -> str:
    for key in ("task_goal", "name", "title", "description"):
        value = str(data.get(key, "")).strip()
        if value:
            return value
    return ""
