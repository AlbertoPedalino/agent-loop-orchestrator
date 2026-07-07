"""Dry-run tests proving target execution boundaries are not crossed."""

from pathlib import Path

import pytest

from agent import orchestrator
from agent.subagents import SubagentConfig


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _write_cleanup_config(
    tmp_path: Path, *, delete_on_success: bool = False, delete_on_failure: bool = False
) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "project:",
                "  name: cleanup-test",
                "backend:",
                "  type: cli",
                "git:",
                f"  delete_worktree_on_success: {str(delete_on_success).lower()}",
                f"  delete_worktree_on_failure: {str(delete_on_failure).lower()}",
                "limits:",
                "  max_changed_files: 8",
                "verification:",
                "  commands: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_resolve_agent_provider_accepts_mapping_and_string_config() -> None:
    assert orchestrator._resolve_agent_provider({}, None) == "claude"
    assert (
        orchestrator._resolve_agent_provider({"agent": {"provider": "codex"}}, None)
        == "codex"
    )
    assert orchestrator._resolve_agent_provider({"agent": "codex"}, None) == "codex"
    assert orchestrator._resolve_agent_provider({"agent": "claude"}, "codex") == "codex"


def test_subscription_cli_mode_rejects_max_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("limits:\n  max_budget_usd: 1\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)

    with pytest.raises(ValueError, match="subscription CLI mode"):
        orchestrator.run_orchestrator(
            repo_path=tmp_path,
            task="test task",
            config_path=config_path,
        )


def test_codex_api_mode_rejects_max_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n  provider: codex\nbackend:\n  type: api\nlimits:\n  max_budget_usd: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)

    with pytest.raises(ValueError, match="agent 'claude'"):
        orchestrator.run_orchestrator(
            repo_path=tmp_path,
            task="test task",
            config_path=config_path,
        )


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


def test_plan_only_reports_preexisting_changed_file_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    dirty_status = " M a.txt\n M b.txt\n?? c.txt"
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: dirty_status)
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda prompt, repo_path, **kwargs: "safe plan",
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="test task",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        plan_only=True,
    )

    assert result.status == "plan-only-complete"
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "**changed files**: 3" in report


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


def test_created_clean_worktree_is_removed_on_success_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "new-worktree"
    config_path = _write_cleanup_config(tmp_path, delete_on_success=True)
    run_dir.mkdir()
    worktree.mkdir()
    removed: list[tuple[Path, Path]] = []
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path, **kwargs: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path, **kwargs: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: None)
    monkeypatch.setattr(orchestrator, "branch_exists", lambda *args: True)
    monkeypatch.setattr(orchestrator, "create_worktree", lambda *args: worktree)
    monkeypatch.setattr(orchestrator, "_run_phase", lambda *, phase, **kwargs: f"{phase} output")
    monkeypatch.setattr(orchestrator, "run_verification_commands", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        orchestrator,
        "remove_worktree",
        lambda repo_path, worktree_path: removed.append((repo_path, worktree_path)) or "",
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="full run",
        config_path=config_path,
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
    )

    assert result.status == "completed"
    assert removed == [(tmp_path.resolve(), worktree.resolve())]
    report = result.report_path.read_text(encoding="utf-8")
    assert "worktree cleanup**: removed" in report


def test_reused_worktree_is_not_removed_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "existing-worktree"
    config_path = _write_cleanup_config(tmp_path, delete_on_success=True)
    run_dir.mkdir()
    worktree.mkdir()
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path, **kwargs: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path, **kwargs: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: worktree)
    monkeypatch.setattr(
        orchestrator,
        "create_worktree",
        lambda *args, **kwargs: pytest.fail("Existing worktree must be reused"),
    )
    monkeypatch.setattr(
        orchestrator,
        "remove_worktree",
        lambda *args, **kwargs: pytest.fail("Reused worktree must not be removed"),
    )
    monkeypatch.setattr(orchestrator, "_run_phase", lambda *, phase, **kwargs: f"{phase} output")
    monkeypatch.setattr(orchestrator, "run_verification_commands", lambda *args, **kwargs: {})

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="full run",
        config_path=config_path,
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
    )

    assert result.status == "completed"
    report = result.report_path.read_text(encoding="utf-8")
    assert "worktree cleanup**: skipped (worktree was reused)" in report


def test_dirty_created_worktree_is_not_removed_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "new-worktree"
    config_path = _write_cleanup_config(tmp_path, delete_on_success=True)
    run_dir.mkdir()
    worktree.mkdir()
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path, **kwargs: " M changed.py")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path, **kwargs: "diff")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: None)
    monkeypatch.setattr(orchestrator, "branch_exists", lambda *args: True)
    monkeypatch.setattr(orchestrator, "create_worktree", lambda *args: worktree)
    monkeypatch.setattr(
        orchestrator,
        "remove_worktree",
        lambda *args, **kwargs: pytest.fail("Dirty worktree must not be removed"),
    )
    monkeypatch.setattr(orchestrator, "_run_phase", lambda *, phase, **kwargs: f"{phase} output")
    monkeypatch.setattr(orchestrator, "run_verification_commands", lambda *args, **kwargs: {})

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="full run",
        config_path=config_path,
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
    )

    assert result.status == "completed"
    report = result.report_path.read_text(encoding="utf-8")
    assert "worktree cleanup**: skipped (working tree dirty)" in report


def test_created_clean_worktree_is_removed_on_failure_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "new-worktree"
    config_path = _write_cleanup_config(tmp_path, delete_on_failure=True)
    run_dir.mkdir()
    worktree.mkdir()
    removed: list[tuple[Path, Path]] = []
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/plan")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path, **kwargs: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path, **kwargs: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: None)
    monkeypatch.setattr(orchestrator, "branch_exists", lambda *args: True)
    monkeypatch.setattr(orchestrator, "create_worktree", lambda *args: worktree)
    monkeypatch.setattr(
        orchestrator,
        "_run_phase",
        lambda *, phase, **kwargs: (_ for _ in ()).throw(RuntimeError("agent failed")),
    )
    monkeypatch.setattr(
        orchestrator,
        "remove_worktree",
        lambda repo_path, worktree_path: removed.append((repo_path, worktree_path)) or "",
    )

    with pytest.raises(RuntimeError, match="agent failed"):
        orchestrator.run_orchestrator(
            repo_path=tmp_path,
            task="full run",
            config_path=config_path,
            use_worktree=True,
            base_branch="base",
            agent_branch="agent/plan",
        )

    assert removed == [(tmp_path.resolve(), worktree.resolve())]
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "worktree cleanup**: removed" in report


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
            agent="claude",
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
        agent="claude",
        backend="cli",
        subagent=subagent,
        max_budget_usd=None,
        blocked_commands=["git push"],
    )

    assert output == "safe plan"
    assert captured["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert captured["permission_mode"] is None


def test_codex_phase_dispatches_to_codex_runner(
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
        lambda *args, **kwargs: pytest.fail("Claude runner must not handle Codex phases"),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_codex_prompt",
        lambda prompt, repo_path, **kwargs: captured.update(kwargs) or "codex plan",
    )

    output = orchestrator._run_phase(
        phase="planner",
        prompt="plan",
        task="task",
        repo_path=tmp_path,
        agent="codex",
        backend="cli",
        subagent=subagent,
        max_budget_usd=None,
        blocked_commands=["git push"],
    )

    assert output == "codex plan"
    assert captured["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert captured["disallowed_tools"] == ["Bash(git push:*)"]


def test_api_claude_phase_receives_max_budget(
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
        agent="claude",
        backend="api",
        subagent=subagent,
        max_budget_usd=1.25,
        blocked_commands=["git push"],
    )

    assert output == "safe plan"
    assert captured["backend"] == "api"
    assert captured["max_budget_usd"] == 1.25


def test_codex_phase_rejects_max_budget_before_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subagent = SubagentConfig(
        "planner", "plan", ["Read", "Grep", "Glob"], 1, PROJECT_ROOT / "prompts" / "planner.md"
    )
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "main")
    monkeypatch.setattr(
        orchestrator,
        "run_codex_prompt",
        lambda *args, **kwargs: pytest.fail("Codex runner must not receive max_budget_usd"),
    )

    with pytest.raises(ValueError, match="Claude phases"):
        orchestrator._run_phase(
            phase="planner",
            prompt="plan",
            task="task",
            repo_path=tmp_path,
            agent="codex",
            backend="api",
            subagent=subagent,
            max_budget_usd=1.25,
            blocked_commands=["git push"],
        )
