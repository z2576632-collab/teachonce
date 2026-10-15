"""Agent Skills export tests."""

from __future__ import annotations

from dataclasses import replace

from teachonce.models import DecisionRule, Skill, SkillVersion
from teachonce.skill_export import export_skill_md, slugify_skill_name


def _skill_and_version() -> tuple[Skill, SkillVersion]:
    skill = Skill.create("Course Material Organizing", "desc")
    sv = SkillVersion(
        skill_id=skill.id,
        version=2,
        trigger_conditions=["整理课程相关文件时"],
        decision_rules=[
            DecisionRule.create(
                condition="归档资料时",
                decision="按课程分文件夹",
                rationale="用户按课查找",
            )
        ],
        exception_handling=[{"situation": "无法判断归属", "handling": "放 _待整理/"}],
        anti_patterns=["按文件类型分类"],
        open_questions=["手机端截图如何处理"],
        summary="整理课程资料的工作",
        changelog="新增录音规则",
        derived_from={"demonstrations": ["demo_1"], "corrections": ["corr_1"]},
        status="approved",
    )
    return skill, sv


def test_slugify_ascii():
    assert slugify_skill_name("Course Material Organizing!") == "course-material-organizing"


def test_slugify_cjk_falls_back():
    assert slugify_skill_name("课程资料整理") == ""


def test_export_has_frontmatter_and_sections():
    skill, sv = _skill_and_version()
    content = export_skill_md(skill, sv)

    assert content.startswith("---\n")
    head = content.split("---")[1]
    assert "name: course-material-organizing" in head
    assert "description:" in head

    assert "# Course Material Organizing" in content
    assert "## When to use (trigger conditions)" in content
    assert "## Decision rules" in content
    assert "按课程分文件夹" in content
    assert "## Exception handling" in content
    assert "## Anti-patterns (avoid)" in content
    assert "## Open questions" in content
    assert "## Provenance" in content
    assert "demo_1" in content
    assert "v2 (approved)" in content


def test_export_cjk_name_with_override():
    skill = Skill.create("课程资料整理")
    _, sv = _skill_and_version()
    sv = replace(sv, skill_id=skill.id)
    content = export_skill_md(skill, sv, name_override="course-organizing")
    assert "name: course-organizing" in content


def test_export_cjk_name_without_override_uses_id_fallback():
    skill = Skill.create("课程资料整理")
    _, sv = _skill_and_version()
    sv = replace(sv, skill_id=skill.id)
    content = export_skill_md(skill, sv)
    assert f"name: teachonce-skill-{skill.id.split('_')[-1]}" in content


def test_frontmatter_description_single_line():
    skill, sv = _skill_and_version()
    content = export_skill_md(skill, sv)
    head = content.split("---")[1].strip().splitlines()
    description_lines = [ln for ln in head if ln.startswith("description:")]
    assert len(description_lines) == 1  # YAML-safe single line
