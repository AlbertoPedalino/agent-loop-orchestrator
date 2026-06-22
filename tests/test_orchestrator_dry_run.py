"""Dry-run tests proving target execution boundaries are not crossed."""

from pathlib import Path

import pytest

from agent import orchestrator
from agent.subagents import SubagentConfig


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_dry_run_does_not_call_claude_or_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "main")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda *args, **kwargs: pytest.fail("Claude must not run in dry-run mode"),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: pytest.fail("Verification must not run in dry-run mode"),
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="test task",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        dry_run=True,
    )

    assert result.status == "dry-run"
    assert (run_dir / "planner_output.md").is_file()
    assert (run_dir / "report.md").is_file()


def test_real_phase_rejects_protected_branch_before_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subagent = SubagentConfig("planner", "plan", ["Read"], 1, PROJECT_ROOT / "prompts" / "planner.md")
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "main")
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda *args, **kwargs: pytest.fail("Claude must not run on protected branch"),
    )

    with pytest.raises(RuntimeError, match="protected branch"):
        orchestrator._run_phase(
            phase="planner",
            prompt="plan",
            task="task",
            repo_path=tmp_path,
            backend="cli",
            subagent=subagent,
            max_budget_usd=None,
        )
