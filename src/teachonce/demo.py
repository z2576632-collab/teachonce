"""End-to-end demo — the "personal course-material organizing" scenario.

Story: a human demonstrates course-material organizing three times →
the AI extracts a skill → a new task arrives and the AI decides on its
own → the user corrects it → a v2 is produced → v2 is evaluated against
v1 on held-out cases → the skill is exported as a standard SKILL.md.

Honesty policy (AGENTS.md §5.6): this demo runs against a REAL LLM.
Without a configured API key it exits with setup instructions — it never
simulates learning results.

Run:
    export TEACHCONE_LLM_API_KEY=sk-...          # required
    export TEACHCONE_LLM_BASE_URL=https://api.deepseek.com/v1   # e.g.
    export TEACHCONE_LLM_MODEL=deepseek-chat                    # e.g.
    python -m demo.run_demo
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from teachonce.correction import CorrectionEngine
from teachonce.evaluation import SkillEvaluator
from teachonce.learning import LearningEngine
from teachonce.llm import LLMConfig, OpenAICompatibleLLM
from teachonce.matching import SkillMatcher
from teachonce.models import EvalReport, MatchResult, SkillVersion
from teachonce.skill_export import export_skill_md
from teachonce.store import SkillStore

from . import scenario

LINE = "=" * 72


def section(title: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TeachOnce end-to-end demo")
    parser.add_argument(
        "--out",
        default="demo_out",
        help="output directory for the demo database and artifacts",
    )
    parser.add_argument(
        "--eval-method",
        choices=["auto", "llm_blind", "llm_judge", "heuristic"],
        default="llm_blind",
        help="held-out evaluation method (auto = llm_judge when an LLM "
        "is configured, else heuristic)",
    )
    args = parser.parse_args(argv)

    config = LLMConfig.from_env()
    if not config.api_key:
        print(
            "This demo runs against a real LLM and no API key is configured.\n\n"
            "Set the environment first (example for DeepSeek):\n"
            "  set TEACHCONE_LLM_API_KEY=sk-...                 (Windows cmd)\n"
            '  $env:TEACHCONE_LLM_API_KEY="sk-..."              (PowerShell)\n'
            "  export TEACHCONE_LLM_API_KEY=sk-...              (bash)\n"
            "  export TEACHCONE_LLM_BASE_URL=https://api.deepseek.com/v1\n"
            "  export TEACHCONE_LLM_MODEL=deepseek-chat\n\n"
            "TeachOnce does not fake demo results without a real LLM.",
        )
        return 2

    print(f"LLM: {config.model} @ {config.base_url}")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) / f"demo_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "teachonce.db"
    print(f"Demo sandbox: {out_dir.resolve()}")

    with SkillStore(db_path) as store:
        llm = OpenAICompatibleLLM(config)
        learning = LearningEngine(store, llm)
        matching = SkillMatcher(store, llm)
        correcting = CorrectionEngine(store, llm)
        evaluating = SkillEvaluator(store, llm)
        summary: list[str] = []

        # ------------------------------------------------------------------
        section("STEP 1/8 — Import 3 demonstrations of the human doing the task")
        demo_ids: list[str] = []
        for idx, payload in enumerate(scenario.SEED_DEMONSTRATIONS, start=1):
            demo = learning.import_demonstration(payload)
            demo_ids.append(demo.id)
            line = f"  [{idx}] {demo.id}  {demo.task_goal}"
            print(line)
            summary.append(f"- imported {demo.id}: {demo.task_goal}")

        # ------------------------------------------------------------------
        section("STEP 2/8 — Extract skill v1 from the demonstrations (LLM)")
        skill, v1 = learning.extract_candidate_skill(scenario.SKILL_NAME, demo_ids)
        print(f"skill_id={skill.id}  name={skill.name}  version=v{v1.version} ({v1.status})")
        _print_version(v1)
        summary.append(f"\n## v1 (candidate)\n\n{_version_markdown(v1)}")

        # ------------------------------------------------------------------
        section("STEP 3/8 — Human review: approve v1 as the active version")
        initial_report = evaluating.evaluate(
            skill.id, 1, 1, scenario.BASELINE_CASES, method=args.eval_method
        )
        _print_report(initial_report)
        if not _review_and_approve(correcting, skill.id, 1, initial_report):
            return 0
        store_skill = store.get_skill(skill.id)
        print(f"active_version = v{store_skill.active_version}  (approved)")

        # ------------------------------------------------------------------
        section("STEP 4/8 — A NEW task arrives (cross-task transfer)")
        print(f"Task: {scenario.NEW_TASK}")
        match = matching.match(scenario.NEW_TASK)
        _print_match(match)
        summary.append(f"\n## Match result for the new task\n\n```\n{match.to_dict()}\n```")

        # ------------------------------------------------------------------
        section("STEP 5/8 — User correction → revised candidate v2 (LLM)")
        print(f'User says: "{scenario.CORRECTION_FEEDBACK}"')
        correction = correcting.record_correction(skill.id, scenario.CORRECTION_FEEDBACK)
        print(f"\nclassified as: {correction.correction_type}\nanalysis: {correction.analysis}")
        v2 = correcting.apply_correction(skill.id)
        print(f"\nv2 produced: v{v2.version} ({v2.status})")
        print(f"\nchangelog: {v2.changelog}")
        print("\nstructured changes:")
        for change in v2.changes:
            print(
                f"  [{change.get('op', '?')}] {change.get('rule_id', '')}: "
                f"{change.get('description', '')}"
            )
        _print_version(v2)
        summary.append(f"\n## v2 (candidate)\n\n{_version_markdown(v2)}")

        # ------------------------------------------------------------------
        section("STEP 6/8 — Evaluate v1 vs v2 on HELD-OUT cases")
        print("(these cases were never part of the learning input)")
        report = evaluating.evaluate(
            skill.id,
            v1.version,
            v2.version,
            scenario.HELD_OUT_CASES,
            method=args.eval_method,
        )
        _print_report(report)
        summary.append(f"\n## Held-out evaluation (v1 vs v2)\n\n{_report_markdown(report)}")

        section("STEP 7/8 — Human review: approve v2 only after evaluation")
        if not _review_and_approve(correcting, skill.id, v2.version, report):
            return 0

        # ------------------------------------------------------------------
        section("STEP 8/8 — Export the active version as Agent Skills SKILL.md")
        active = store.get_active_version(skill.id)
        assert active is not None
        content = export_skill_md(
            store.get_skill(skill.id), active, name_override="course-material-organizing"
        )
        skill_md_path = out_dir / "SKILL.md"
        skill_md_path.write_text(content, encoding="utf-8")
        print(f"saved to: {skill_md_path.resolve()}\n")
        print(content)

        # ------------------------------------------------------------------
        summary_path = out_dir / "summary.md"
        header = (
            f"# TeachOnce demo run — {run_id}\n\n"
            f"- LLM: `{config.model}` @ `{config.base_url}`\n"
            f"- skill: `{skill.id}` ({scenario.SKILL_NAME})\n"
            f"- evaluation method: `{report.method}`\n\n"
            "> Every step below was executed at run time against the real LLM\n"
            "> configured above. Held-out cases were never given to the\n"
            "> learning or correction steps. Nothing here is a canned output.\n"
        )
        summary_path.write_text(header + "\n".join(summary) + "\n", encoding="utf-8")

        section("Demo complete")
        print(f"Artifacts: {out_dir.resolve()}")
        print("  - teachonce.db   (all versions, corrections, reports — local only)")
        print("  - SKILL.md       (exportable Agent Skill)")
        print("  - summary.md     (this run's record)")
        print(
            "\nNote: TeachOnce did NOT touch any real files. The skill outputs\n"
            "judgments and plans; execution stays with the host agent and you."
        )
        return 0


# ---------------------------------------------------------------------------
# printing helpers
# ---------------------------------------------------------------------------


def _review_and_approve(
    engine: CorrectionEngine, skill_id: str, version: int, report: EvalReport
) -> bool:
    review = engine.review_version(skill_id, version)
    eligible = next((r["eligible"] for r in review["evaluations"] if r["id"] == report.id), False)
    if not eligible:
        print(
            "Candidate remains inactive: regression evidence is not eligible. Inspect the saved report."
        )
        return False
    if not sys.stdin.isatty():
        print(
            "Candidate saved for review. Resume in an interactive terminal or use teachonce review/approve with this database."
        )
        return False
    if (
        input(f"Review v{version} and report {report.id}. Type APPROVE to activate: ").strip()
        != "APPROVE"
    ):
        print("Candidate kept for later review; active version unchanged.")
        return False
    engine.approve_version(
        skill_id,
        version,
        evaluation_id=report.id,
        confirmed=True,
        reviewer="interactive demo user",
        reason="Reviewed displayed candidate and per-case evidence",
    )
    return True


def _print_version(sv: SkillVersion) -> None:
    print("\ntrigger conditions:")
    for cond in sv.trigger_conditions:
        print(f"  - {cond}")
    print("decision rules:")
    for rule in sv.decision_rules:
        print(f"  - when {rule.condition}")
        print(f"    -> {rule.decision}")
        if rule.rationale:
            print(f"       (why: {rule.rationale})")
    if sv.exception_handling:
        print("exception handling:")
        for exc in sv.exception_handling:
            print(f"  - {exc.get('situation', '')} -> {exc.get('handling', '')}")
    if sv.anti_patterns:
        print("anti-patterns:")
        for ap in sv.anti_patterns:
            print(f"  - {ap}")
    if sv.open_questions:
        print("open questions:")
        for q in sv.open_questions:
            print(f"  - {q}")


def _print_match(match: MatchResult) -> None:
    print(f"\nmatched: {match.matched}")
    if match.skill_id:
        print(f"skill: {match.skill_name} ({match.skill_id}) v{match.version}")
    print(f"confidence: {match.confidence:.2f}")
    print(f"requires human confirmation: {match.requires_confirmation}")
    print(f"rationale: {match.rationale}")
    if match.plan:
        print("proposed plan (advisory — execution stays with the host agent):")
        for step in match.plan:
            print(f"  - {step}")
    if match.missing_info:
        print("missing info:")
        for item in match.missing_info:
            print(f"  - {item}")


def _print_report(report: EvalReport) -> None:
    print(f"\nmethod: {report.method}")
    print(
        f"score v{report.version_a}: {report.score_a:.0%}   score v{report.version_b}: {report.score_b:.0%}"
    )
    print("\nper-case:")
    for r in report.results:
        mark_a = "PASS" if r["supported_a"] else "FAIL"
        mark_b = "PASS" if r["supported_b"] else "FAIL"
        print(
            f"  case {r['case_index'] + 1}: v{report.version_a}={mark_a}  v{report.version_b}={mark_b}"
        )
        print(f"    situation: {r['situation']}")
        print(f"    v{report.version_a} detail: {r['detail_a']}")
        print(f"    v{report.version_b} detail: {r['detail_b']}")
    print(f"\nmethod notes: {report.notes}")
    if not any(r["supported_a"] and not r["supported_b"] for r in report.results) and any(
        (not r["supported_a"]) and r["supported_b"] for r in report.results
    ):
        print(
            f"\nverdict: v{report.version_b} is not worse than v{report.version_a} "
            "and fixes at least one previously failing case."
        )
    else:
        print(
            f"\nverdict: v{report.version_b} vs v{report.version_a} = "
            f"{report.score_b:.0%} vs {report.score_a:.0%} — inspect the "
            "per-case details above."
        )


def _version_markdown(sv: SkillVersion) -> str:
    lines = [f"v{sv.version} ({sv.status}) — {sv.summary}"]
    lines.append("\ntrigger conditions:")
    for cond in sv.trigger_conditions:
        lines.append(f"- {cond}")
    lines.append("\ndecision rules:")
    for rule in sv.decision_rules:
        entry = f"- when {rule.condition} -> {rule.decision}"
        if rule.rationale:
            entry += f" (why: {rule.rationale})"
        lines.append(entry)
    if sv.changes:
        lines.append("\nchanges in this revision:")
        for ch in sv.changes:
            lines.append(
                f"- [{ch.get('op', '?')}] {ch.get('rule_id', '')}: {ch.get('description', '')}"
            )
    if sv.open_questions:
        lines.append("\nopen questions:")
        for q in sv.open_questions:
            lines.append(f"- {q}")
    return "\n".join(lines)


def _report_markdown(report: EvalReport) -> str:
    lines = [
        f"- method: `{report.method}`",
        f"- score v{report.version_a}: **{report.score_a:.0%}**",
        f"- score v{report.version_b}: **{report.score_b:.0%}**",
        "",
        "| case | situation | v_a | v_b |",
        "|---|---|---|---|",
    ]
    for r in report.results:
        mark_a = "PASS" if r["supported_a"] else "FAIL"
        mark_b = "PASS" if r["supported_b"] else "FAIL"
        situation = str(r["situation"]).replace("|", "/")
        lines.append(f"| {r['case_index'] + 1} | {situation} | {mark_a} | {mark_b} |")
    lines.append("")
    lines.append(f"Method notes: {report.notes}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
