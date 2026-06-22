"""Tests for task input and safety CLI validation."""

from pathlib import Path
import sys

import pytest

from agent import main as main_module
from agent.main import build_parser


def test_parser_accepts_task_and_dry_run() -> None:
    args = build_parser().parse_args(["--repo-path", ".", "--task", "task", "--dry-run"])
    assert args.task == "task"
    assert args.dry_run


def test_parser_accepts_plan_only() -> None:
    args = build_parser().parse_args(["--repo-path", ".", "--task", "task", "--plan-only"])

    assert args.plan_only
    assert not args.setup_only


def test_parser_accepts_task_file(tmp_path: Path) -> None:
    task_file = tmp_path / "task.md"
    args = build_parser().parse_args(["--repo-path", ".", "--task-file", str(task_file)])
    assert args.task_file == task_file


def test_parser_rejects_both_or_missing_task() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--repo-path", "."])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--repo-path", ".", "--task", "a", "--task-file", "b"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--repo-path", ".", "--task", "task", "--plan-only", "--setup-only"]
        )


def test_main_rejects_protected_agent_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent.main", "--repo-path", ".", "--task", "task", "--agent-branch", "main"],
    )

    with pytest.raises(SystemExit) as exit_code:
        main_module.main()

    assert exit_code.value.code == 2
