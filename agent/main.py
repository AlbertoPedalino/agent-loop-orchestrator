"""Command-line entry point for the controlled orchestration loop."""

from __future__ import annotations

import argparse
from pathlib import Path

from agent.git_utils import is_protected_branch
from agent.orchestrator import resolve_config_selection, run_orchestrator


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Run a controlled Claude Code orchestration loop.")
    parser.add_argument("--repo-path", type=Path, required=True, help="Path to the target Git repository.")
    task_group = parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument("--task", help="Description of the requested change.")
    task_group.add_argument("--task-file", type=Path, help="UTF-8 file containing the requested task.")
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Explicit YAML configuration file. When omitted, discover "
            ".agent-loop/config.yaml or .agent-loop.yaml in the target repository."
        ),
    )
    parser.add_argument("--backend", choices=("cli", "sdk"), help="Override configured backend.")
    parser.add_argument("--max-fix-attempts", type=int, help="Override limits.max_fix_attempts.")
    parser.add_argument("--dry-run", action="store_true", help="Write run metadata without target changes.")
    parser.add_argument(
        "--use-worktree",
        action="store_true",
        default=None,
        help="Create an isolated local worktree (disabled in dry-run).",
    )
    parser.add_argument("--worktree-root", type=Path, help="Directory for local worktrees.")
    parser.add_argument("--base-branch", help="Base branch for a new worktree branch.")
    parser.add_argument("--agent-branch", help="New local agent branch for the worktree.")
    parser.add_argument("--remote", help="Git remote name (default: config or origin).")
    parser.add_argument(
        "--subagents-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "subagents.default.yaml",
        help="Subagent YAML configuration file.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--setup-only",
        action="store_true",
        help="Prepare/report worktree setup without invoking planner or other Claude phases.",
    )
    mode_group.add_argument(
        "--plan-only",
        action="store_true",
        help="Run or simulate only the planner phase, then stop before implementation.",
    )
    return parser


def _task_from_args(args: argparse.Namespace) -> str:
    if args.task is not None:
        return args.task
    try:
        return args.task_file.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not read task file {args.task_file}: {error}") from error


def main() -> int:
    """Parse CLI arguments and execute the selected orchestration mode."""
    parser = build_parser()
    args = parser.parse_args()
    if args.max_fix_attempts is not None and args.max_fix_attempts < 0:
        parser.error("--max-fix-attempts must be zero or greater")
    if args.agent_branch and is_protected_branch(args.agent_branch):
        parser.error(f"--agent-branch is protected: {args.agent_branch}")

    try:
        task = _task_from_args(args)
        config_selection = resolve_config_selection(args.repo_path, args.config)
        result = run_orchestrator(
            repo_path=args.repo_path,
            task=task,
            config_path=config_selection.path,
            config_source=config_selection.source,
            max_fix_attempts=args.max_fix_attempts,
            dry_run=args.dry_run,
            backend=args.backend,
            use_worktree=args.use_worktree,
            worktree_root=args.worktree_root,
            base_branch=args.base_branch,
            agent_branch=args.agent_branch,
            remote=args.remote,
            subagents_config_path=args.subagents_config,
            setup_only=args.setup_only,
            plan_only=args.plan_only,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as error:
        parser.error(str(error))

    print(f"Run directory: {result.run_dir}")
    print(f"Report: {result.report_path}")
    print(f"Status: {result.status}")
    print(f"Config: {config_selection.path} ({config_selection.source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
