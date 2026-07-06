"""Command-line interface for the file-based task queue.

Usage::

    python -m agent.queue_cli add tasks/my-task.yaml [--queue-dir tasks/queue]
    python -m agent.queue_cli run [--workers 2] [--max-tasks 10] [--max-minutes 60]
    python -m agent.queue_cli list [--queue-dir tasks/queue]

``add`` validates a task file and copies it into the queue; ``run`` processes
queued tasks with one or more workers until the queue drains or a limit is
reached; ``list`` prints the queue contents per state.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agent.log import configure_logging
from agent.queue import DEFAULT_QUEUE_DIR, enqueue, list_queue, run_queue


def build_parser() -> argparse.ArgumentParser:
    """Build the queue command-line parser."""
    parser = argparse.ArgumentParser(description="Manage the orchestrator task queue.")
    parser.add_argument(
        "--queue-dir",
        type=Path,
        default=DEFAULT_QUEUE_DIR,
        help=f"Queue root directory (default: {DEFAULT_QUEUE_DIR}).",
    )
    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument("--verbose", action="store_true", help="Show DEBUG logs.")
    log_group.add_argument("--quiet", action="store_true", help="Warnings and errors only.")

    subcommands = parser.add_subparsers(dest="command", required=True)

    add_parser = subcommands.add_parser("add", help="Validate and enqueue a task file.")
    add_parser.add_argument("task_file", type=Path, help="Queue task YAML file to enqueue.")

    run_parser = subcommands.add_parser("run", help="Process queued tasks.")
    run_parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers; more than 1 requires worktree-isolated tasks.",
    )
    run_parser.add_argument(
        "--max-tasks", type=int, help="Stop after claiming this many task attempts."
    )
    run_parser.add_argument(
        "--max-minutes", type=float, help="Stop claiming new tasks after this many minutes."
    )
    run_parser.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        default=True,
        help="Disable live streaming of agent output.",
    )

    subcommands.add_parser("list", help="Show queue contents per state.")
    return parser


def main() -> int:
    """Parse arguments and execute the selected queue command."""
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(verbose=args.verbose, quiet=args.quiet)

    try:
        if args.command == "add":
            # A task file without repo_path targets the repository you launch
            # `add` from, mirroring run-file cwd semantics; the queued copy is
            # stamped so workers stay location-independent.
            destination = enqueue(args.queue_dir, args.task_file, default_repo_path=Path.cwd())
            print(f"Enqueued: {destination}")
        elif args.command == "run":
            summary = run_queue(
                args.queue_dir,
                workers=args.workers,
                max_tasks=args.max_tasks,
                max_minutes=args.max_minutes,
                stream=args.stream,
            )
            print(f"Succeeded: {summary.succeeded}")
            print(f"Failed: {summary.failed}")
            print(f"Retried: {summary.retried}")
            print(f"Revised: {summary.revised}")
            print(f"Stopped: {summary.stopped_reason}")
            return 0 if summary.failed == 0 else 1
        else:
            for state, names in list_queue(args.queue_dir).items():
                print(f"{state} ({len(names)}):")
                for name in names:
                    print(f"  {name}")
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
