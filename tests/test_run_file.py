"""Tests for run-file loading, CLI wiring, and artifact recording."""

from pathlib import Path
import sys
import textwrap

import pytest

from agent import main as main_module
from agent import orchestrator
from agent.orchestrator import OrchestrationResult
from agent.run_file import RunFileConfig, load_run_file, resolve_task_text


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _write_run_file(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_valid_run_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: ../external/GM-Board
        backend: cli
        use_worktree: true
        base_branch: feature/x
        agent_branch: agent/inspect
        plan_only: true
        task: |
          Inspect the structure.
        """,
    )

    config = load_run_file(run_file)

    assert isinstance(config, RunFileConfig)
    assert config.repo_path == (tmp_path / ".." / "external" / "GM-Board").resolve()
    assert config.agent == "claude"
    assert config.backend == "cli"
    assert config.use_worktree is True
    assert config.base_branch == "feature/x"
    assert config.agent_branch == "agent/inspect"
    assert config.plan_only is True
    assert config.setup_only is False
    assert config.dry_run is False
    assert config.config is None
    assert config.task is not None and "Inspect the structure." in config.task
    assert config.task_file is None


def test_relative_paths_resolve_against_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: target
        task: do work
        """,
    )

    config = load_run_file(run_file)

    assert config.repo_path == (workdir / "target").resolve()


def test_repo_path_optional_in_run_file(tmp_path: Path) -> None:
    run_file = _write_run_file(tmp_path / "run.yaml", "task: do work\n")
    config = load_run_file(run_file)
    assert config.repo_path is None


def test_empty_repo_path_fails(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: "   "
        task: do work
        """,
    )
    with pytest.raises(ValueError, match="repo_path"):
        load_run_file(run_file)


def test_missing_task_and_task_file_fails(tmp_path: Path) -> None:
    run_file = _write_run_file(tmp_path / "run.yaml", "repo_path: .\n")
    with pytest.raises(ValueError, match="either 'task' or 'task_file'"):
        load_run_file(run_file)


def test_task_and_task_file_together_fail(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: inline
        task_file: task.md
        """,
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_run_file(run_file)


def test_default_backend_is_cli(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: do work
        """,
    )
    assert load_run_file(run_file).backend == "cli"


def test_api_backend_is_loaded(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: do work
        backend: api
        """,
    )
    assert load_run_file(run_file).backend == "api"


def test_default_agent_is_claude(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: do work
        """,
    )
    assert load_run_file(run_file).agent == "claude"


def test_codex_agent_is_loaded(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        agent: codex
        task: do work
        """,
    )
    assert load_run_file(run_file).agent == "codex"


def test_sdk_backend_is_not_supported(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        agent: claude
        backend: sdk
        task: do work
        """,
    )
    with pytest.raises(ValueError, match="backend"):
        load_run_file(run_file)


def test_invalid_agent_fails(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        agent: bogus
        task: do work
        """,
    )
    with pytest.raises(ValueError, match="agent"):
        load_run_file(run_file)


def test_invalid_backend_fails(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        backend: bogus
        task: do work
        """,
    )
    with pytest.raises(ValueError, match="backend"):
        load_run_file(run_file)


def test_booleans_default_false(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: do work
        """,
    )
    config = load_run_file(run_file)
    assert config.use_worktree is False
    assert config.plan_only is False
    assert config.setup_only is False
    assert config.dry_run is False


def test_non_boolean_flag_fails(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: do work
        plan_only: "yep"
        """,
    )
    with pytest.raises(ValueError, match="plan_only"):
        load_run_file(run_file)


def test_unknown_field_fails(tmp_path: Path) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: do work
        bogus: 1
        """,
    )
    with pytest.raises(ValueError, match="Unknown run-file fields: bogus"):
        load_run_file(run_file)


def test_missing_run_file_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Run file not found"):
        load_run_file(tmp_path / "absent.yaml")


def test_resolve_task_text_reads_task_file(tmp_path: Path) -> None:
    task_file = tmp_path / "task.md"
    task_file.write_text("file task body", encoding="utf-8")
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        f"""
        repo_path: .
        task_file: {task_file}
        """,
    )
    config = load_run_file(run_file)
    assert config.task is None
    assert config.task_file == task_file.resolve()
    assert resolve_task_text(config) == "file task body"


def test_cli_run_file_invokes_same_orchestrator_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        f"""
        repo_path: {target}
        backend: cli
        plan_only: true
        task: inspect things
        """,
    )
    captured: dict[str, object] = {}

    def fake_run_orchestrator(**kwargs: object) -> OrchestrationResult:
        captured.update(kwargs)
        return OrchestrationResult(tmp_path, tmp_path / "report.md", "plan-only-complete", target)

    monkeypatch.setattr(main_module, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(sys, "argv", ["agent.main", "--run-file", str(run_file)])

    assert main_module.main() == 0
    assert captured["repo_path"] == target.resolve()
    assert captured["repo_path_source"] == "run-file repo_path"
    assert captured["task"] == "inspect things"
    assert captured["agent"] == "claude"
    assert captured["backend"] == "cli"
    assert captured["plan_only"] is True
    assert captured["launched_from"] == "run-file"
    assert captured["run_file_path"] == run_file.resolve()


def _capture_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], target: Path
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_run_orchestrator(**kwargs: object) -> OrchestrationResult:
        captured.update(kwargs)
        return OrchestrationResult(tmp_path, tmp_path / "report.md", "dry-run", target)

    monkeypatch.setattr(main_module, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(sys, "argv", argv)
    assert main_module.main() == 0
    return captured


def test_run_file_without_repo_path_defaults_to_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "GM-Board"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _write_run_file(workdir / "run.yaml", "task: inspect\n")

    captured = _capture_invocation(
        monkeypatch, tmp_path, ["agent.main", "--run-file", "run.yaml"], workdir
    )

    assert captured["repo_path"] == Path.cwd()
    assert captured["repo_path"] == workdir.resolve()
    assert captured["repo_path_source"] == "current working directory"


def test_explicit_repo_path_overrides_cwd_and_run_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "GM-Board"
    workdir.mkdir()
    override = tmp_path / "override-target"
    override.mkdir()
    monkeypatch.chdir(workdir)
    _write_run_file(
        workdir / "run.yaml",
        f"""
        repo_path: {tmp_path / "ignored"}
        task: inspect
        """,
    )

    captured = _capture_invocation(
        monkeypatch,
        tmp_path,
        ["agent.main", "--run-file", "run.yaml", "--repo-path", str(override)],
        override,
    )

    assert captured["repo_path"] == override
    assert captured["repo_path_source"] == "cli --repo-path"


def test_run_file_repo_path_used_when_provided(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        f"""
        repo_path: {target}
        task: inspect
        """,
    )

    captured = _capture_invocation(
        monkeypatch, tmp_path, ["agent.main", "--run-file", str(run_file)], target
    )

    assert captured["repo_path"] == target.resolve()
    assert captured["repo_path_source"] == "run-file repo_path"


def test_target_local_config_discovery_from_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "GM-Board"
    (workdir / ".agent-loop" / "tasks").mkdir(parents=True)
    (workdir / ".agent-loop" / "config.yaml").write_text("project: {}\n", encoding="utf-8")
    run_file = workdir / ".agent-loop" / "tasks" / "inspect.yaml"
    _write_run_file(run_file, "task: inspect\n")
    monkeypatch.chdir(workdir)

    captured = _capture_invocation(
        monkeypatch,
        tmp_path,
        ["agent.main", "--run-file", ".agent-loop/tasks/inspect.yaml"],
        workdir,
    )

    assert captured["repo_path_source"] == "current working directory"
    assert captured["config_path"] == (workdir / ".agent-loop" / "config.yaml").resolve()
    assert captured["config_source"] == "target-local .agent-loop/config.yaml"


def test_cli_run_file_incompatible_with_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: inspect
        """,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent.main", "--run-file", str(run_file), "--task", "override"],
    )
    with pytest.raises(SystemExit) as exit_code:
        main_module.main()
    assert exit_code.value.code == 2


def test_cli_run_file_incompatible_with_setup_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        """
        repo_path: .
        task: inspect
        """,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent.main", "--run-file", str(run_file), "--setup-only"],
    )
    with pytest.raises(SystemExit) as exit_code:
        main_module.main()
    assert exit_code.value.code == 2


def test_run_file_target_local_config_discovery_when_config_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    (target / ".agent-loop").mkdir(parents=True)
    (target / ".agent-loop" / "config.yaml").write_text("project: {}\n", encoding="utf-8")
    run_file = _write_run_file(
        tmp_path / "run.yaml",
        f"""
        repo_path: {target}
        task: inspect
        """,
    )
    captured: dict[str, object] = {}

    def fake_run_orchestrator(**kwargs: object) -> OrchestrationResult:
        captured.update(kwargs)
        return OrchestrationResult(tmp_path, tmp_path / "report.md", "dry-run", target)

    monkeypatch.setattr(main_module, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(sys, "argv", ["agent.main", "--run-file", str(run_file)])

    assert main_module.main() == 0
    assert captured["config_path"] == (target / ".agent-loop" / "config.yaml").resolve()
    assert captured["config_source"] == "target-local .agent-loop/config.yaml"


def test_run_file_path_recorded_in_report_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_file_path = tmp_path / "run.yaml"
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "main")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda *args, **kwargs: pytest.fail("Claude must not run in dry-run mode"),
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="test task",
        config_path=PROJECT_ROOT / "configs" / "default.yaml",
        dry_run=True,
        run_file_path=run_file_path,
        launched_from="run-file",
        repo_path_source="current working directory",
    )

    run_source = (run_dir / "run_source.md").read_text(encoding="utf-8")
    assert "Launched from**: run-file" in run_source
    assert str(run_file_path) in run_source
    assert "Repo path source**: current working directory" in run_source
    assert str(tmp_path.resolve()) in run_source
    report = result.report_path.read_text(encoding="utf-8")
    assert "launched from**: run-file" in report
    assert str(run_file_path) in report
    assert "repo path source**: current working directory" in report
