"""Tests for task input and safety CLI validation."""

from pathlib import Path
import sys

import pytest

from agent import main as main_module
from agent.orchestrator import ConfigSelection, OrchestrationResult
from agent.main import build_parser


def test_parser_accepts_task_and_dry_run() -> None:
    args = build_parser().parse_args(["--repo-path", ".", "--task", "task", "--dry-run"])
    assert args.task == "task"
    assert args.dry_run


def test_parser_accepts_plan_only() -> None:
    args = build_parser().parse_args(["--repo-path", ".", "--task", "task", "--plan-only"])

    assert args.plan_only
    assert not args.setup_only


def test_parser_accepts_agent_provider_flags() -> None:
    assert (
        build_parser().parse_args(["--repo-path", ".", "--task", "task", "-claude"]).agent
        == "claude"
    )
    assert (
        build_parser().parse_args(["--repo-path", ".", "--task", "task", "-codex"]).agent
        == "codex"
    )
    assert (
        build_parser().parse_args(["--repo-path", ".", "--task", "task", "--agent", "codex"]).agent
        == "codex"
    )


def test_parser_accepts_task_file(tmp_path: Path) -> None:
    task_file = tmp_path / "task.md"
    args = build_parser().parse_args(["--repo-path", ".", "--task-file", str(task_file)])
    assert args.task_file == task_file


def test_parser_rejects_sdk_backend() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--repo-path", ".", "--task", "task", "--backend", "sdk"])


def test_parser_accepts_api_backend() -> None:
    args = build_parser().parse_args(["--repo-path", ".", "--task", "task", "--backend", "api"])

    assert args.backend == "api"


def test_parser_rejects_both_task_and_mode_conflicts() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--repo-path", ".", "--task", "a", "--task-file", "b"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--repo-path", ".", "--task", "task", "--plan-only", "--setup-only"]
        )


def test_parser_accepts_run_file() -> None:
    args = build_parser().parse_args(["--run-file", "run.yaml"])
    assert args.run_file == Path("run.yaml")
    assert args.repo_path is None


def test_main_rejects_missing_task_without_run_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["agent.main", "--repo-path", "."])
    with pytest.raises(SystemExit) as exit_code:
        main_module.main()
    assert exit_code.value.code == 2


def test_main_rejects_missing_repo_path_without_run_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["agent.main", "--task", "task"])
    with pytest.raises(SystemExit) as exit_code:
        main_module.main()
    assert exit_code.value.code == 2


def test_main_rejects_protected_agent_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent.main", "--repo-path", ".", "--task", "task", "--agent-branch", "main"],
    )

    with pytest.raises(SystemExit) as exit_code:
        main_module.main()

    assert exit_code.value.code == 2


def test_main_passes_codex_agent_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run_orchestrator(**kwargs: object) -> OrchestrationResult:
        captured.update(kwargs)
        return OrchestrationResult(tmp_path, tmp_path / "report.md", "dry-run", tmp_path)

    monkeypatch.setattr(
        main_module,
        "resolve_config_selection",
        lambda *args: ConfigSelection(tmp_path / "config.yaml", "test"),
    )
    monkeypatch.setattr(main_module, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent.main", "--repo-path", str(tmp_path), "--task", "task", "-codex"],
    )

    assert main_module.main() == 0
    assert captured["agent"] == "codex"


@pytest.mark.parametrize("status", ["verification-failed", "review-revise", "review-rejected"])
def test_main_returns_nonzero_for_unsuccessful_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
) -> None:
    monkeypatch.setattr(
        main_module,
        "resolve_config_selection",
        lambda *args: ConfigSelection(tmp_path / "config.yaml", "test"),
    )
    monkeypatch.setattr(
        main_module,
        "run_orchestrator",
        lambda **kwargs: OrchestrationResult(tmp_path, tmp_path / "report.md", status, tmp_path),
    )
    monkeypatch.setattr(
        sys, "argv", ["agent.main", "--repo-path", str(tmp_path), "--task", "task"]
    )

    assert main_module.main() == 1
