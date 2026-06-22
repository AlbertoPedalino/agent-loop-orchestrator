"""Command-line entry point for the controlled orchestration loop."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from agent.git_utils import is_protected_branch
from agent.orchestrator import resolve_config_selection, run_orchestrator
from agent.run_file import load_run_file, resolve_task_text


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Run a controlled Claude Code orchestration loop.")
    parser.add_argument(
        "--repo-path",
        type=Path,
        help="Path to the target Git repository (required unless --run-file is used).",
    )
    parser.add_argument(
        "--run-file",
        type=Path,
        help=(
            "YAML run file providing every run parameter. Cannot be combined with "
            "other run flags such as --task, --task-file, --backend, or --plan-only."
        ),
    )
    task_group = parser.add_mutually_exclusive_group()
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
    parser.add_argument(
        "--branch-mode",
        choices=("worktree", "in_place", "none"),
        help=(
            "How to place the agent branch: 'worktree' (separate linked worktree), "
            "'in_place' (agent branch in the current target repo), or 'none' (run on "
            "the current branch without creating one)."
        ),
    )
    parser.add_argument(
        "--create-branch",
        choices=("auto", "always", "never"),
        help=(
            "When in-place branch mode creates the agent branch: 'auto' (only for a "
            "full implementation loop), 'always', or 'never'."
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        default=None,
        help="Permit switching branches when the working tree is dirty.",
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


@dataclass(frozen=True)
class _RunInvocation:
    """Run parameters resolved from either CLI arguments or a run file."""

    repo_path: Path
    repo_path_source: str
    task: str
    config_path: Path | None
    backend: str | None
    use_worktree: bool | None
    branch_mode: str | None
    create_branch: str | None
    allow_dirty: bool | None
    base_branch: str | None
    agent_branch: str | None
    plan_only: bool
    setup_only: bool
    dry_run: bool
    remote: str | None
    worktree_root: Path | None
    max_fix_attempts: int | None
    run_file_path: Path | None
    launched_from: str


def _task_from_args(args: argparse.Namespace) -> str:
    if args.task is not None:
        return args.task
    try:
        return args.task_file.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not read task file {args.task_file}: {error}") from error


def _conflicting_run_file_flags(args: argparse.Namespace) -> list[str]:
    """Return CLI run-parameter flags that conflict with --run-file.

    ``--repo-path`` is intentionally excluded: it is the one supported override,
    taking precedence over a run file's ``repo_path`` and over the working
    directory default.
    """
    conflicts: list[str] = []
    optional_value_flags = (
        ("--task", args.task),
        ("--task-file", args.task_file),
        ("--config", args.config),
        ("--backend", args.backend),
        ("--use-worktree", args.use_worktree),
        ("--branch-mode", args.branch_mode),
        ("--create-branch", args.create_branch),
        ("--allow-dirty", args.allow_dirty),
        ("--base-branch", args.base_branch),
        ("--agent-branch", args.agent_branch),
        ("--remote", args.remote),
        ("--worktree-root", args.worktree_root),
        ("--max-fix-attempts", args.max_fix_attempts),
    )
    conflicts.extend(name for name, value in optional_value_flags if value is not None)
    conflicts.extend(
        name
        for name, value in (
            ("--plan-only", args.plan_only),
            ("--setup-only", args.setup_only),
            ("--dry-run", args.dry_run),
        )
        if value
    )
    return conflicts


def _resolve_run_invocation(args: argparse.Namespace, parser: argparse.ArgumentParser) -> _RunInvocation:
    """Build run parameters from a run file or directly from CLI arguments.

    ``--run-file`` is mutually exclusive with every other run-parameter flag
    except ``--repo-path``, so the source of each parameter stays unambiguous.
    The target repository is resolved in this order: an explicit ``--repo-path``,
    then the run file's ``repo_path``, then the current working directory. To
    dry-run or plan-only a run file, set ``dry_run`` or ``plan_only`` in the file.
    """
    if args.run_file is not None:
        conflicts = _conflicting_run_file_flags(args)
        if conflicts:
            parser.error(
                "--run-file cannot be combined with other run flags: " + ", ".join(conflicts)
            )
        run = load_run_file(args.run_file)
        if args.repo_path is not None:
            repo_path, repo_path_source = args.repo_path, "cli --repo-path"
        elif run.repo_path is not None:
            repo_path, repo_path_source = run.repo_path, "run-file repo_path"
        else:
            repo_path, repo_path_source = Path.cwd(), "current working directory"
        return _RunInvocation(
            repo_path=repo_path,
            repo_path_source=repo_path_source,
            task=resolve_task_text(run),
            config_path=run.config,
            backend=run.backend,
            use_worktree=run.use_worktree,
            branch_mode=run.branch_mode,
            create_branch=run.create_branch,
            allow_dirty=run.allow_dirty,
            base_branch=run.base_branch,
            agent_branch=run.agent_branch,
            plan_only=run.plan_only,
            setup_only=run.setup_only,
            dry_run=run.dry_run,
            remote=None,
            worktree_root=None,
            max_fix_attempts=None,
            run_file_path=args.run_file.expanduser().resolve(),
            launched_from="run-file",
        )

    if args.repo_path is None:
        parser.error("--repo-path is required unless --run-file is used")
    if args.task is None and args.task_file is None:
        parser.error("one of --task, --task-file, or --run-file is required")
    return _RunInvocation(
        repo_path=args.repo_path,
        repo_path_source="cli --repo-path",
        task=_task_from_args(args),
        config_path=args.config,
        backend=args.backend,
        use_worktree=args.use_worktree,
        branch_mode=args.branch_mode,
        create_branch=args.create_branch,
        allow_dirty=args.allow_dirty,
        base_branch=args.base_branch,
        agent_branch=args.agent_branch,
        plan_only=args.plan_only,
        setup_only=args.setup_only,
        dry_run=args.dry_run,
        remote=args.remote,
        worktree_root=args.worktree_root,
        max_fix_attempts=args.max_fix_attempts,
        run_file_path=None,
        launched_from="cli-args",
    )


def main() -> int:
    """Parse CLI arguments and execute the selected orchestration mode."""
    parser = build_parser()
    args = parser.parse_args()
    if args.max_fix_attempts is not None and args.max_fix_attempts < 0:
        parser.error("--max-fix-attempts must be zero or greater")
    if args.agent_branch and is_protected_branch(args.agent_branch):
        parser.error(f"--agent-branch is protected: {args.agent_branch}")

    try:
        invocation = _resolve_run_invocation(args, parser)
        config_selection = resolve_config_selection(invocation.repo_path, invocation.config_path)
        result = run_orchestrator(
            repo_path=invocation.repo_path,
            task=invocation.task,
            config_path=config_selection.path,
            config_source=config_selection.source,
            max_fix_attempts=invocation.max_fix_attempts,
            dry_run=invocation.dry_run,
            backend=invocation.backend,
            use_worktree=invocation.use_worktree,
            branch_mode=invocation.branch_mode,
            create_branch=invocation.create_branch,
            allow_dirty=invocation.allow_dirty,
            worktree_root=invocation.worktree_root,
            base_branch=invocation.base_branch,
            agent_branch=invocation.agent_branch,
            remote=invocation.remote,
            subagents_config_path=args.subagents_config,
            setup_only=invocation.setup_only,
            plan_only=invocation.plan_only,
            run_file_path=invocation.run_file_path,
            launched_from=invocation.launched_from,
            repo_path_source=invocation.repo_path_source,
        )
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as error:
        parser.error(str(error))

    print(f"Run directory: {result.run_dir}")
    print(f"Report: {result.report_path}")
    print(f"Status: {result.status}")
    print(f"Config: {config_selection.path} ({config_selection.source})")
    print(f"Launched from: {invocation.launched_from}")
    print(f"Repo path: {invocation.repo_path} ({invocation.repo_path_source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
