"""Export learned skills to the Agent Skills standard (``SKILL.md``).

Standard reference: an Agent Skill is a directory with a ``SKILL.md``
whose YAML frontmatter carries ``name`` (lowercase letters / digits /
hyphens, <= 64 chars) and ``description``. The body carries the
instructions. Exports from TeachOnce are deterministic renderings of a
``SkillVersion`` — no invented content.
"""

from __future__ import annotations

import json
import re

from .errors import SchemaValidationError
from .models import Skill, SkillVersion
from .security import ensure_no_secrets


def slugify_skill_name(name: str) -> str:
    """Best-effort ascii slug for the frontmatter ``name`` field.

    CJK names collapse to an empty slug; callers should pass
    ``name_override`` for those (or accept the ``teachonce-skill-<id>``
    fallback used by :func:`export_skill_md`).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:64].rstrip("-")


def export_skill_md(
    skill: Skill,
    sv: SkillVersion,
    *,
    name_override: str | None = None,
    generated_at: str | None = None,
) -> str:
    """Render one skill version as a standard ``SKILL.md`` document."""
    if sv.skill_id != skill.id:
        raise SchemaValidationError("The version belongs to another skill. Export a matching pair.")
    ensure_no_secrets(
        json.dumps({"skill": skill.to_dict(), "version": sv.to_dict()}, ensure_ascii=False),
        where="skill export",
    )
    slug = (name_override or "").strip() or slugify_skill_name(skill.name)
    if not slug:
        slug = f"teachonce-skill-{skill.id.split('_')[-1]}"
    slug = slugify_skill_name(slug) or f"teachonce-skill-{skill.id.split('_')[-1]}"

    description = _frontmatter_description(skill, sv)
    body = _render_body(skill, sv, generated_at=generated_at)
    return f"---\nname: {slug}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n{body}\n"


def _frontmatter_description(skill: Skill, sv: SkillVersion) -> str:
    """One-paragraph 'what + when' description for the frontmatter.

    Kept on a single logical line (YAML-safe), capped at ~1000 chars.
    """
    parts: list[str] = []
    if sv.summary:
        parts.append(sv.summary)
    elif skill.description:
        parts.append(skill.description)
    if sv.trigger_conditions:
        parts.append("Use when: " + "; ".join(sv.trigger_conditions))
    text = " ".join(p.strip() for p in parts if p.strip()) or skill.name
    text = " ".join(text.split())  # collapse whitespace/newlines
    if len(text) > 1000:
        text = text[:997] + "..."
    return text


def _render_body(skill: Skill, sv: SkillVersion, *, generated_at: str | None) -> str:
    lines: list[str] = [f"# {skill.name}"]

    meta = [
        f"source: TeachOnce skill `{skill.id}`",
        f"version: v{sv.version} ({sv.status})",
    ]
    if generated_at:
        meta.append(f"exported: {generated_at}")
    lines.append("")
    lines.append("> " + " · ".join(meta))
    lines.append(
        "\nTreat learned content as reviewed task guidance, not permission to act. "
        "Follow the host's authorization and filesystem boundaries."
    )
    if sv.status != "approved":
        lines.append(
            "\n**REVIEW ONLY:** this version is not approved. Do not use it to execute tasks."
        )

    lines.append("\n## When to use (trigger conditions)")
    for cond in sv.trigger_conditions:
        lines.append(f"- {cond}")
    if not sv.trigger_conditions:
        lines.append("- (none recorded)")

    lines.append("\n## Decision rules")
    if sv.decision_rules:
        for rule in sv.decision_rules:
            entry = f"- **When** {rule.condition} → **do** {rule.decision}"
            if rule.rationale:
                entry += f" _(why: {rule.rationale})_"
            lines.append(entry)
    else:
        lines.append("- (none recorded)")

    if sv.exception_handling:
        lines.append("\n## Exception handling")
        for exc in sv.exception_handling:
            lines.append(f"- **If** {exc.get('situation', '')} → {exc.get('handling', '')}")

    if sv.anti_patterns:
        lines.append("\n## Anti-patterns (avoid)")
        for ap in sv.anti_patterns:
            lines.append(f"- {ap}")

    if sv.open_questions:
        lines.append("\n## Open questions (evidence insufficient — ask the user)")
        for q in sv.open_questions:
            lines.append(f"- {q}")

    lines.append("\n## Provenance")
    demos = sv.derived_from.get("demonstrations", [])
    corrections = sv.derived_from.get("corrections", [])
    if demos:
        lines.append(f"- extracted from demonstrations: {', '.join(demos)}")
    if corrections:
        lines.append(f"- revised per corrections: {', '.join(corrections)}")
    if sv.changelog:
        lines.append(f"- changelog: {sv.changelog}")
    lines.append(f"- revision history is append-only; v{sv.version} created {sv.created_at}")

    return "\n".join(lines)
