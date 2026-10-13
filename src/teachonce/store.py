"""SQLite persistence layer — append-only skill versions, local-first.

Design notes (see docs/ARCHITECTURE.md):
- One file, zero ops. Complex structures stored as JSON text columns.
- ``skill_versions`` is append-only: updates insert new rows, never UPDATE.
- ``skills.active_version`` is the only mutable pointer, moved by explicit
  approve / rollback operations in ``correction.py``.
- Default location ``~/.teachonce/teachonce.db`` (override: ``TEACHCONE_HOME``).
  Personal learning records stay local by default — no network calls here.

Thread safety: the MCP server (SDK v2) executes synchronous tools in worker
threads, so one connection is shared across threads. All access is
serialized through a reentrant lock and the connection is opened with
``check_same_thread=False``.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .errors import NotFoundError
from .models import (
    Correction,
    Demonstration,
    EvalReport,
    MatchResult,
    Skill,
    SkillVersion,
    utc_now,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demonstrations (
    id TEXT PRIMARY KEY,
    task_goal TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]',
    key_judgments_json TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    trigger_conditions_json TEXT NOT NULL DEFAULT '[]',
    decision_rules_json TEXT NOT NULL DEFAULT '[]',
    exception_handling_json TEXT NOT NULL DEFAULT '[]',
    anti_patterns_json TEXT NOT NULL DEFAULT '[]',
    changelog TEXT NOT NULL DEFAULT '',
    changes_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate',
    derived_from_json TEXT NOT NULL DEFAULT '{}',
    open_questions_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, version),
    FOREIGN KEY (skill_id) REFERENCES skills(id)
);

CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    feedback TEXT NOT NULL,
    correction_type TEXT NOT NULL DEFAULT 'other',
    analysis TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    applied_in_version INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES skills(id)
);

CREATE TABLE IF NOT EXISTS eval_reports (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    version_a INTEGER NOT NULL,
    version_b INTEGER NOT NULL,
    method TEXT NOT NULL,
    score_a REAL NOT NULL,
    score_b REAL NOT NULL,
    results_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES skills(id)
);

CREATE TABLE IF NOT EXISTS match_logs (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    skill_id TEXT,
    version INTEGER,
    confidence REAL NOT NULL DEFAULT 0.0,
    requires_confirmation INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def _locked(method):
    """Serialize access to the shared SQLite connection.

    Reentrant because some public methods call others internally
    (e.g. ``get_active_version`` -> ``get_skill``).
    """

    @functools.wraps(method)
    def wrapper(self: SkillStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def default_db_path() -> Path:
    """Resolve the default database path.

    ``TEACHCONE_HOME`` overrides the base directory (useful for tests and
    demo sandboxes); otherwise ``~/.teachonce``.
    """
    home = os.environ.get("TEACHCONE_HOME")
    base = Path(home) if home else Path.home() / ".teachonce"
    return base / "teachonce.db"


class SkillStore:
    """SQLite-backed store for demonstrations, skills, corrections, reports."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @_locked
    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # demonstrations
    # ------------------------------------------------------------------

    @_locked
    def add_demonstration(self, demo: Demonstration) -> str:
        self._conn.execute(
            "INSERT INTO demonstrations (id, task_goal, context, steps_json,"
            " key_judgments_json, outcome, tags_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                demo.id,
                demo.task_goal,
                demo.context,
                json.dumps([s.to_dict() for s in demo.steps], ensure_ascii=False),
                json.dumps(demo.key_judgments, ensure_ascii=False),
                demo.outcome,
                json.dumps(demo.tags, ensure_ascii=False),
                demo.created_at,
            ),
        )
        self._conn.commit()
        return demo.id

    @_locked
    def get_demonstration(self, demo_id: str) -> Demonstration:
        row = self._conn.execute("SELECT * FROM demonstrations WHERE id = ?", (demo_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Demonstration '{demo_id}' not found.")
        return _row_to_demonstration(row)

    @_locked
    def list_demonstrations(self) -> list[Demonstration]:
        rows = self._conn.execute("SELECT * FROM demonstrations ORDER BY created_at").fetchall()
        return [_row_to_demonstration(r) for r in rows]

    # ------------------------------------------------------------------
    # skills
    # ------------------------------------------------------------------

    @_locked
    def create_skill(self, skill: Skill) -> str:
        self._conn.execute(
            "INSERT INTO skills (id, name, description, active_version,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                skill.id,
                skill.name,
                skill.description,
                skill.active_version,
                skill.created_at,
                skill.updated_at,
            ),
        )
        self._conn.commit()
        return skill.id

    @_locked
    def get_skill(self, skill_id: str) -> Skill:
        row = self._conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Skill '{skill_id}' not found.")
        return Skill(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            active_version=row["active_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @_locked
    def list_skills(self) -> list[Skill]:
        rows = self._conn.execute("SELECT * FROM skills ORDER BY created_at").fetchall()
        return [
            Skill(
                id=r["id"],
                name=r["name"],
                description=r["description"],
                active_version=r["active_version"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    @_locked
    def update_skill_meta(self, skill_id: str, *, description: str | None = None) -> None:
        """Update mutable skill metadata (name/description). Versions immutable."""
        if description is not None:
            self._conn.execute(
                "UPDATE skills SET description = ?, updated_at = ? WHERE id = ?",
                (description, utc_now(), skill_id),
            )
            self._conn.commit()

    @_locked
    def next_version_number(self, skill_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM skill_versions WHERE skill_id = ?",
            (skill_id,),
        ).fetchone()
        return int(row["v"]) + 1

    # ------------------------------------------------------------------
    # skill versions (append-only)
    # ------------------------------------------------------------------

    @_locked
    def add_skill_version(self, sv: SkillVersion) -> None:
        if self._conn.execute(
            "SELECT 1 FROM skill_versions WHERE skill_id = ? AND version = ?",
            (sv.skill_id, sv.version),
        ).fetchone():
            raise ValueError(
                f"Version {sv.version} of skill '{sv.skill_id}' already exists. "
                "Versions are append-only: bump the version number; never "
                "overwrite history."
            )
        self._conn.execute(
            "INSERT INTO skill_versions (skill_id, version, trigger_conditions_json,"
            " decision_rules_json, exception_handling_json, anti_patterns_json,"
            " changelog, changes_json, status, derived_from_json, open_questions_json,"
            " summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sv.skill_id,
                sv.version,
                json.dumps(sv.trigger_conditions, ensure_ascii=False),
                json.dumps([r.to_dict() for r in sv.decision_rules], ensure_ascii=False),
                json.dumps(sv.exception_handling, ensure_ascii=False),
                json.dumps(sv.anti_patterns, ensure_ascii=False),
                sv.changelog,
                json.dumps(sv.changes, ensure_ascii=False),
                sv.status,
                json.dumps(sv.derived_from, ensure_ascii=False),
                json.dumps(sv.open_questions, ensure_ascii=False),
                sv.summary,
                sv.created_at,
            ),
        )
        self._conn.commit()

    @_locked
    def get_skill_version(self, skill_id: str, version: int) -> SkillVersion:
        row = self._conn.execute(
            "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?",
            (skill_id, version),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Version {version} of skill '{skill_id}' not found.")
        return _row_to_skill_version(row)

    @_locked
    def list_skill_versions(self, skill_id: str) -> list[SkillVersion]:
        rows = self._conn.execute(
            "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY version",
            (skill_id,),
        ).fetchall()
        return [_row_to_skill_version(r) for r in rows]

    @_locked
    def get_active_version(self, skill_id: str) -> SkillVersion | None:
        """The version the matcher may use; None until a version is approved."""
        skill = self.get_skill(skill_id)
        if skill.active_version is None:
            return None
        return self.get_skill_version(skill_id, skill.active_version)

    @_locked
    def set_version_status(self, skill_id: str, version: int, status: str) -> None:
        cur = self._conn.execute(
            "UPDATE skill_versions SET status = ? WHERE skill_id = ? AND version = ?",
            (status, skill_id, version),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"Version {version} of skill '{skill_id}' not found.")
        self._conn.commit()

    @_locked
    def set_active_version(self, skill_id: str, version: int | None) -> None:
        self._conn.execute(
            "UPDATE skills SET active_version = ?, updated_at = ? WHERE id = ?",
            (version, utc_now(), skill_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # corrections
    # ------------------------------------------------------------------

    @_locked
    def add_correction(self, correction: Correction) -> str:
        self._conn.execute(
            "INSERT INTO corrections (id, skill_id, target_version, feedback,"
            " correction_type, analysis, status, applied_in_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                correction.id,
                correction.skill_id,
                correction.target_version,
                correction.feedback,
                correction.correction_type,
                correction.analysis,
                correction.status,
                correction.applied_in_version,
                correction.created_at,
            ),
        )
        self._conn.commit()
        return correction.id

    @_locked
    def get_correction(self, correction_id: str) -> Correction:
        row = self._conn.execute(
            "SELECT * FROM corrections WHERE id = ?", (correction_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Correction '{correction_id}' not found.")
        return _row_to_correction(row)

    @_locked
    def list_corrections(self, skill_id: str, *, status: str | None = None) -> list[Correction]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM corrections WHERE skill_id = ? ORDER BY created_at",
                (skill_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM corrections WHERE skill_id = ? AND status = ? ORDER BY created_at",
                (skill_id, status),
            ).fetchall()
        return [_row_to_correction(r) for r in rows]

    @_locked
    def mark_correction_applied(self, correction_id: str, version: int) -> None:
        self._conn.execute(
            "UPDATE corrections SET status = 'applied', applied_in_version = ? WHERE id = ?",
            (version, correction_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # eval reports
    # ------------------------------------------------------------------

    @_locked
    def save_eval_report(self, report: EvalReport) -> str:
        self._conn.execute(
            "INSERT INTO eval_reports (id, skill_id, version_a, version_b, method,"
            " score_a, score_b, results_json, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.id,
                report.skill_id,
                report.version_a,
                report.version_b,
                report.method,
                report.score_a,
                report.score_b,
                json.dumps(report.results, ensure_ascii=False),
                report.notes,
                report.created_at,
            ),
        )
        self._conn.commit()
        return report.id

    @_locked
    def list_eval_reports(self, skill_id: str) -> list[EvalReport]:
        rows = self._conn.execute(
            "SELECT * FROM eval_reports WHERE skill_id = ? ORDER BY created_at",
            (skill_id,),
        ).fetchall()
        reports: list[EvalReport] = []
        for r in rows:
            reports.append(
                EvalReport(
                    id=r["id"],
                    skill_id=r["skill_id"],
                    version_a=r["version_a"],
                    version_b=r["version_b"],
                    method=r["method"],
                    score_a=r["score_a"],
                    score_b=r["score_b"],
                    results=json.loads(r["results_json"]),
                    notes=r["notes"],
                    created_at=r["created_at"],
                )
            )
        return reports

    # ------------------------------------------------------------------
    # match logs
    # ------------------------------------------------------------------

    @_locked
    def log_match(self, result: MatchResult) -> str:
        from .models import new_id

        log_id = new_id("match")
        self._conn.execute(
            "INSERT INTO match_logs (id, task, skill_id, version, confidence,"
            " requires_confirmation, payload_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                log_id,
                result.task,
                result.skill_id,
                result.version,
                result.confidence,
                1 if result.requires_confirmation else 0,
                json.dumps(result.to_dict(), ensure_ascii=False),
                result.created_at,
            ),
        )
        self._conn.commit()
        return log_id

    @_locked
    def list_match_logs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM match_logs ORDER BY created_at").fetchall()
        return [
            {
                "id": r["id"],
                "task": r["task"],
                "skill_id": r["skill_id"],
                "version": r["version"],
                "confidence": r["confidence"],
                "requires_confirmation": bool(r["requires_confirmation"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# row converters
# ---------------------------------------------------------------------------


def _row_to_demonstration(row: sqlite3.Row) -> Demonstration:
    return Demonstration.from_dict(
        {
            "id": row["id"],
            "task_goal": row["task_goal"],
            "context": row["context"],
            "steps": json.loads(row["steps_json"]),
            "key_judgments": json.loads(row["key_judgments_json"]),
            "outcome": row["outcome"],
            "tags": json.loads(row["tags_json"]),
            "created_at": row["created_at"],
        }
    )


def _row_to_skill_version(row: sqlite3.Row) -> SkillVersion:
    return SkillVersion.from_dict(
        {
            "skill_id": row["skill_id"],
            "version": row["version"],
            "trigger_conditions": json.loads(row["trigger_conditions_json"]),
            "decision_rules": json.loads(row["decision_rules_json"]),
            "exception_handling": json.loads(row["exception_handling_json"]),
            "anti_patterns": json.loads(row["anti_patterns_json"]),
            "changelog": row["changelog"],
            "changes": json.loads(row["changes_json"]),
            "status": row["status"],
            "derived_from": json.loads(row["derived_from_json"]),
            "open_questions": json.loads(row["open_questions_json"]),
            "summary": row["summary"],
            "created_at": row["created_at"],
        }
    )


def _row_to_correction(row: sqlite3.Row) -> Correction:
    return Correction.from_dict(
        {
            "id": row["id"],
            "skill_id": row["skill_id"],
            "target_version": row["target_version"],
            "feedback": row["feedback"],
            "correction_type": row["correction_type"],
            "analysis": row["analysis"],
            "status": row["status"],
            "applied_in_version": row["applied_in_version"],
            "created_at": row["created_at"],
        }
    )
