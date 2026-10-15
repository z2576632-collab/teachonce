"""Human CLI: local inspection, learning and explicit reviewed activation."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from .correction import CorrectionEngine
from .errors import TeachOnceError
from .evaluation import SkillEvaluator
from .learning import LearningEngine
from .llm import LLMConfig, OpenAICompatibleLLM
from .matching import SkillMatcher
from .skill_export import export_skill_md
from .store import SkillStore, default_db_path


def doctor() -> dict[str, Any]:
    """Inspect setup without sending requests, revealing credentials or creating a database."""
    config = LLMConfig.from_env()
    return {
        "python": sys.version.split()[0],
        "python_supported": sys.version_info >= (3, 12),
        "package": importlib.metadata.version("teachonce"),
        "mcp_sdk": importlib.metadata.version("mcp"),
        "database": str(default_db_path()),
        "llm_configured": bool(config.api_key),
        "host_workflow_ready": True,
        "api_learning_ready": bool(config.api_key),
        "advice": "Run teachonce probe to test stdio. Use explicit host proposals or configure TEACHONCE_LLM_API_KEY for API inference. "
        "Codex account sign-in does not provide this package's LLM API credentials.",
    }


def codex_config() -> str:
    """Render a Codex stdio MCP entry using this installed Python; never embed API keys."""
    env_names = [
        "TEACHONCE_HOME",
        "TEACHONCE_LLM_API_KEY",
        "TEACHONCE_LLM_BASE_URL",
        "TEACHONCE_LLM_MODEL",
        "TEACHONCE_GUI_OBSERVATION",
        "TEACHCONE_HOME",
        "TEACHCONE_LLM_API_KEY",
        "TEACHCONE_LLM_BASE_URL",
        "TEACHCONE_LLM_MODEL",
        "TEACHCONE_GUI_OBSERVATION",
    ]
    return (
        "[mcp_servers.teachonce]\ncommand = "
        + json.dumps(sys.executable)
        + '\nargs = ["-m", "teachonce.mcp_server"]\nenv_vars = '
        + json.dumps(env_names)
        + "\nstartup_timeout_sec = 20\ntool_timeout_sec = 180\n"
        + '\n[mcp_servers.teachonce.tools.teachonce_approve_version]\napproval_mode = "prompt"\n'
        + '\n[mcp_servers.teachonce.tools.teachonce_rollback]\napproval_mode = "prompt"\n'
    )


async def probe() -> dict[str, Any]:
    """Launch the actual installed stdio server and verify local discovery without a model."""
    import tempfile

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    with tempfile.TemporaryDirectory(prefix="teachonce-probe-") as temp:
        env = dict(os.environ)
        env["TEACHONCE_HOME"] = temp
        env["PYTHONIOENCODING"] = "utf-8"
        async with Client(
            StdioServerParameters(
                command=sys.executable,
                args=["-m", "teachonce.mcp_server"],
                env=env,
            )
        ) as client:
            listed = await client.list_tools()
            tools = [tool.name for tool in listed.tools]
            return {
                "transport": "stdio",
                "connected": True,
                "tools": tools,
                "tool_count": len(tools),
                "llm_called": False,
            }


def main(argv: list[str] | None = None) -> int:
    """Run a command and return an actionable exit code; emit no raw model/error payloads."""
    parser = argparse.ArgumentParser(prog="teachonce")
    parser.add_argument("--db", type=Path, help="Explicit local SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("probe")
    sub.add_parser("codex-config")
    sub.add_parser("serve")
    demo = sub.add_parser("demo", help="Interactive real-provider demonstration")
    demo.add_argument("--out", default="demo_out")
    demo.add_argument(
        "--eval-method", choices=["llm_blind", "llm_judge", "heuristic"], default="llm_blind"
    )
    install = sub.add_parser("install-skill")
    install.add_argument(
        "--dest", type=Path, required=True, help="Skill parent, e.g. .agents/skills"
    )
    imp = sub.add_parser("import")
    imp.add_argument("input", type=Path)
    gen = sub.add_parser("generate")
    gen.add_argument("name")
    gen.add_argument("--demonstrations", nargs="+", required=True)
    match = sub.add_parser("match")
    match.add_argument("task")
    correct = sub.add_parser("correct")
    correct.add_argument("skill_id")
    correct.add_argument("feedback")
    revise = sub.add_parser("revise")
    revise.add_argument("skill_id")
    revise.add_argument("--base", type=int)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("skill_id")
    evaluate.add_argument("version_a", type=int)
    evaluate.add_argument("version_b", type=int)
    evaluate.add_argument("cases", type=Path)
    evaluate.add_argument(
        "--method", choices=["llm_blind", "llm_judge", "heuristic"], default="llm_blind"
    )
    review = sub.add_parser("review")
    review.add_argument("skill_id")
    review.add_argument("version", type=int)
    for name in ("approve", "rollback"):
        cmd = sub.add_parser(name)
        cmd.add_argument("skill_id")
        cmd.add_argument("version", type=int)
        cmd.add_argument("--reviewer", required=True)
        cmd.add_argument("--reason", required=True)
        cmd.add_argument("--yes", action="store_true", help="I reviewed this action with the human")
        if name == "approve":
            cmd.add_argument("--evaluation", required=True)
    export = sub.add_parser("export")
    export.add_argument("skill_id")
    export.add_argument("--version", type=int)
    export.add_argument("--name")
    export.add_argument("--output", type=Path, required=True, help="NEW skill parent directory")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    store: SkillStore | None = None
    try:
        if args.command == "demo":
            from .demo import main as demo_main

            return demo_main(["--out", args.out, "--eval-method", args.eval_method])
        if args.command == "doctor":
            result = doctor()
        elif args.command == "codex-config":
            print(codex_config(), end="")
            return 0
        elif args.command == "probe":
            import asyncio

            result = asyncio.run(probe())
        elif args.command == "install-skill":
            source = files("teachonce").joinpath("skills/teachonce/SKILL.md")
            target = args.dest / "teachonce"
            target.mkdir(parents=True, exist_ok=False)
            (target / "SKILL.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            result = {"installed": str(target.absolute()), "available": "next Codex turn"}
        elif args.command == "serve":
            from .mcp_server import build_server

            store = SkillStore(args.db)
            build_server(store=store).run()
            return 0
        else:
            store = SkillStore(args.db)
            engine = CorrectionEngine(store)

            def llm() -> OpenAICompatibleLLM:
                return OpenAICompatibleLLM.from_env()

            match args.command:
                case "import":
                    result = (
                        LearningEngine(store)
                        .import_demonstration(json.loads(args.input.read_text(encoding="utf-8")))
                        .to_dict()
                    )
                case "generate":
                    skill, version = LearningEngine(store, llm()).extract_candidate_skill(
                        args.name, args.demonstrations
                    )
                    result = {"skill": skill.to_dict(), "version": version.to_dict()}
                case "match":
                    result = SkillMatcher(store, llm()).match(args.task).to_dict()
                case "correct":
                    result = (
                        CorrectionEngine(store, llm())
                        .record_correction(args.skill_id, args.feedback)
                        .to_dict()
                    )
                case "revise":
                    result = (
                        CorrectionEngine(store, llm())
                        .apply_correction(args.skill_id, base_version=args.base)
                        .to_dict()
                    )
                case "evaluate":
                    result = (
                        SkillEvaluator(store, llm() if args.method != "heuristic" else None)
                        .evaluate(
                            args.skill_id,
                            args.version_a,
                            args.version_b,
                            json.loads(args.cases.read_text(encoding="utf-8")),
                            method=args.method,
                        )
                        .to_dict()
                    )
                case "review":
                    result = engine.review_version(args.skill_id, args.version)
                case "approve" | "rollback":
                    kwargs = {
                        "confirmed": args.yes,
                        "reviewer": args.reviewer,
                        "reason": args.reason,
                    }
                    if args.command == "approve":
                        result = engine.approve_version(
                            args.skill_id, args.version, evaluation_id=args.evaluation, **kwargs
                        ).to_dict()
                    else:
                        result = engine.rollback_to(args.skill_id, args.version, **kwargs).to_dict()
                case "export":
                    skill = store.get_skill(args.skill_id)
                    version = (
                        store.get_skill_version(skill.id, args.version)
                        if args.version
                        else store.get_active_version(skill.id)
                    )
                    if version is None:
                        raise TeachOnceError(
                            "No active version. Review and approve a candidate first."
                        )
                    content = export_skill_md(skill, version, name_override=args.name)
                    slug = content.splitlines()[1].removeprefix("name: ")
                    target = args.output / slug
                    target.mkdir(parents=True, exist_ok=False)
                    (target / "SKILL.md").write_text(content, encoding="utf-8")
                    result = {"exported": str(target.absolute()), "status": version.status}
                case "list":
                    result = {"skills": [s.to_dict() for s in store.list_skills()]}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (TeachOnceError, OSError, ValueError, TypeError):
        # Exception chains / OS paths / remote bodies may contain personal input.
        error = sys.exc_info()[1]
        message = (
            str(error)
            if isinstance(error, TeachOnceError)
            else "Invalid input or local filesystem operation. Check the path and JSON format."
        )
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
