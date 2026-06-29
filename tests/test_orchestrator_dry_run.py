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


def test_plan_only_dry_run_writes_only_planner_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "main")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda *args, **kwargs: pytest.fail("Claude must not run in dry-run mode"),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: pytest.fail("Verification must not run in plan-only mode"),
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="test task",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        dry_run=True,
        plan_only=True,
    )

    assert result.status == "dry-run-plan-only"
    assert (run_dir / "planner_prompt.md").is_file()
    assert (run_dir / "planner_output.md").is_file()
    assert (run_dir / "git_status.txt").is_file()
    assert (run_dir / "git_diff.patch").is_file()
    assert not (run_dir / "implementer_output.md").exists()
    assert not (run_dir / "reviewer_output.md").exists()
    assert not (run_dir / "verification_attempt_1.txt").exists()


def test_plan_only_runs_planner_and_skips_later_phases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    phases: list[str] = []
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda prompt, repo_path, **kwargs: phases.append(kwargs["phase"]) or "safe plan",
    )
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: pytest.fail("Verification must not run in plan-only mode"),
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="test task",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        plan_only=True,
    )

    assert result.status == "plan-only-complete"
    assert phases == ["planner"]
    assert (run_dir / "planner_output.md").read_text(encoding="utf-8") == "safe plan\n"
    assert (run_dir / "report.md").is_file()


def test_plan_only_writes_memory_from_emitted_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda *args, **kwargs: "Report body\n```memory\n# Project Memory\nEntry point: app.py\n```",
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="inspect",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        plan_only=True,
    )

    assert result.status == "plan-only-complete"
    memory_file = tmp_path / ".agent-loop" / "memory.md"
    assert memory_file.read_text(encoding="utf-8") == "# Project Memory\nEntry point: app.py\n"


def test_memory_injected_into_planner_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (tmp_path / ".agent-loop").mkdir()
    (tmp_path / ".agent-loop" / "memory.md").write_text(
        "# Project Memory\nKnown: builder lives in src/", encoding="utf-8"
    )
    prompts: list[str] = []
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda prompt, repo_path, **kwargs: prompts.append(prompt) or "ok",
    )

    orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="inspect",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        plan_only=True,
    )

    assert "Accumulated Project Memory" in prompts[0]
    assert "builder lives in src/" in prompts[0]
    assert "Memory Update (required)" in prompts[0]


def test_plan_only_reuses_existing_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "existing-worktree"
    run_dir.mkdir()
    worktree.mkdir()
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: worktree)
    monkeypatch.setattr(
        orchestrator,
        "create_worktree",
        lambda *args, **kwargs: pytest.fail("Existing worktree must be reused"),
    )
    monkeypatch.setattr(orchestrator, "run_claude_prompt", lambda *args, **kwargs: "safe plan")

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="test task",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        plan_only=True,
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
    )

    assert result.target_repo_path == worktree
    assert result.worktree_path == worktree


def test_plan_only_creates_worktree_when_no_matching_worktree_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "new-worktree"
    run_dir.mkdir()
    worktree.mkdir()
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: None)
    monkeypatch.setattr(orchestrator, "branch_exists", lambda *args: True)
    monkeypatch.setattr(orchestrator, "create_worktree", lambda *args: worktree)
    monkeypatch.setattr(orchestrator, "run_claude_prompt", lambda *args, **kwargs: "safe plan")

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="test task",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        plan_only=True,
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
    )

    assert result.worktree_path == worktree


def test_setup_only_dry_run_still_skips_planner(
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
        lambda *args, **kwargs: pytest.fail("Setup-only must not call Claude"),
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="test task",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        dry_run=True,
        setup_only=True,
    )

    assert result.status == "dry-run-setup"
    assert not (run_dir / "planner_output.md").exists()


def test_setup_only_reuses_existing_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "existing-worktree"
    run_dir.mkdir()
    worktree.mkdir()
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: worktree)
    monkeypatch.setattr(
        orchestrator,
        "create_worktree",
        lambda *args, **kwargs: pytest.fail("Existing worktree must be reused"),
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="setup only",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
        setup_only=True,
    )

    assert result.status == "setup-complete"
    assert result.target_repo_path == worktree


def test_full_run_reuses_existing_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "existing-worktree"
    run_dir.mkdir()
    worktree.mkdir()
    phases: list[str] = []
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: worktree)
    monkeypatch.setattr(
        orchestrator,
        "create_worktree",
        lambda *args, **kwargs: pytest.fail("Existing worktree must be reused"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_phase",
        lambda *, phase, **kwargs: phases.append(phase) or f"{phase} output",
    )
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: {"pytest -q": "exit code: 0\nstdout:\n\nstderr:\n"},
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="full run",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
    )

    assert result.status == "completed"
    assert result.target_repo_path == worktree
    assert phases == ["planner", "implementer", "reviewer"]


def test_full_run_creates_worktree_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "new-worktree"
    run_dir.mkdir()
    worktree.mkdir()
    phases: list[str] = []
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: None)
    monkeypatch.setattr(orchestrator, "branch_exists", lambda *args: True)
    monkeypatch.setattr(orchestrator, "create_worktree", lambda *args: worktree)
    monkeypatch.setattr(
        orchestrator,
        "_run_phase",
        lambda *, phase, **kwargs: phases.append(phase) or f"{phase} output",
    )
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: {"pytest -q": "exit code: 0\nstdout:\n\nstderr:\n"},
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="full run",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
    )

    assert result.status == "completed"
    assert result.worktree_path == worktree
    assert phases == ["planner", "implementer", "reviewer"]


def test_write_phase_rejects_protected_branch_before_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subagent = SubagentConfig(
        "implementer", "impl", ["Edit", "Bash"], 1, PROJECT_ROOT / "prompts" / "implementer.md"
    )
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "main")
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda *args, **kwargs: pytest.fail("Claude must not run on protected branch"),
    )

    with pytest.raises(RuntimeError, match="protected branch"):
        orchestrator._run_phase(
            phase="implementer",
            prompt="impl",
            task="task",
            repo_path=tmp_path,
            backend="cli",
            subagent=subagent,
            max_budget_usd=None,
        )


def test_read_only_phase_allowed_on_protected_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subagent = SubagentConfig(
        "planner", "plan", ["Read", "Grep", "Glob"], 1, PROJECT_ROOT / "prompts" / "planner.md"
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "main")
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda prompt, repo_path, **kwargs: captured.update(kwargs) or "safe plan",
    )

    output = orchestrator._run_phase(
        phase="planner",
        prompt="plan",
        task="task",
        repo_path=tmp_path,
        backend="cli",
        subagent=subagent,
        max_budget_usd=None,
        blocked_commands=["git push"],
    )

    assert output == "safe plan"
    assert captured["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert captured["permission_mode"] is None
