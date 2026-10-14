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
