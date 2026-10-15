"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from teachonce.store import SkillStore


@pytest.fixture()
def store(tmp_path):
    """A fresh SkillStore on a temp database."""
    s = SkillStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def reviewed_approval():
    """Exercise real evaluation and approval with an independent synthetic regression case."""
    from teachonce.evaluation import SkillEvaluator

    def approve(engine, skill_id, version):
        skill = engine.store.get_skill(skill_id)
        sv = engine.store.get_skill_version(skill_id, version)
        report = SkillEvaluator(engine.store).evaluate(
            skill_id,
            skill.active_version or version,
            version,
            [
                {
                    "situation": f"Independent review fixture for version {version}",
                    "expected_decision": sv.decision_rules[0].decision,
                    "keywords": [sv.decision_rules[0].decision],
                }
            ],
            method="heuristic",
        )
        return engine.approve_version(
            skill_id,
            version,
            evaluation_id=report.id,
            confirmed=True,
            reviewer="test human",
            reason="Reviewed fixture",
        )

    return approve
