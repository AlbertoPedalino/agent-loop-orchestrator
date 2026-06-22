"""Command-line entry point for the orchestrator skeleton."""

from __future__ import annotations

import argparse
from pathlib import Path

from agent.orchestrator import run_orchestrator


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Prepare a controlled Claude Code orchestration run."
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        required=True,
        help="Path to the target repository.",
    )
    parser.add_argument(
        "--task",
        required=True,
        help="Description of the requested change.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/default.yaml"),
        help="YAML configuration file (default: configs/default.yaml).",
    )
    parser.add_argument(
        "--max-fix-attempts",
        type=int,
        default=None,
        help="Override limits.max_fix_attempts from the configuration.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create run metadata without invoking Claude or changing the target repository.",
    )
    return parser


def main() -> int:
    """Parse arguments and create an orchestration run."""
    parser = build_parser()
    args = parser.parse_args()

    if args.max_fix_attempts is not None and args.max_fix_attempts < 0:
        parser.error("--max-fix-attempts must be zero or greater")

    try:
        result = run_orchestrator(
            repo_path=args.repo_path,
            task=args.task,
            config_path=args.config,
            max_fix_attempts=args.max_fix_attempts,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        parser.error(str(error))

    print(f"Run directory: {result.run_dir}")
    print(f"Report: {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
