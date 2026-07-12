"""End-to-end tests for approved cumulative task checkpoints."""

from pathlib import Path
import subprocess

import pytest

from agent import orchestrator


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("verdict", "expected_status", "expected_commits"),
    [
        ("approve", "completed", "1"),
        ("revise", "review-revise", "0"),
        ("reject", "review-rejected", "0"),
    ],
)
def test_only_approved_task_commits_tests_on_agent_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdict: str,
    expected_status: str,
    expected_commits: str,
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "agent-loop@test")
    _git(repo, "config", "user.name", "Agent Loop Test")
    (repo / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    main_revision = _git(repo, "rev-parse", "main")

    config = tmp_path / "config.yaml"
    config.write_text(
        "project:\n"
        "  use_worktree: false\n"
        "git:\n"
        "  commit_on_success: true\n"
        "memory:\n"
        "  enabled: false\n"
        "  history: false\n"
        "testing:\n"
        "  policy: required\n"
        "  paths: [tests/**]\n"
        "  forbid_test_deletion: true\n"
        "verification:\n"
        "  commands: [git diff --check]\n",
        encoding="utf-8",
    )
    run_dirs = [tmp_path / "run-1", tmp_path / "run-2"]
    for run_dir in run_dirs:
        run_dir.mkdir()
    run_dir_iterator = iter(run_dirs)
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda root: next(run_dir_iterator))
    reviewer_verdict = [verdict]

    def fake_phase(*, phase: str, repo_path: Path, **kwargs: object) -> str:
        if phase == "implementer":
            (repo_path / "base.py").write_text("VALUE = 2\n", encoding="utf-8")
            tests = repo_path / "tests"
            tests.mkdir(exist_ok=True)
            (tests / "test_base.py").write_text(
                "from base import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
                encoding="utf-8",
            )
        if phase == "reviewer":
            return (
                "reviewed\n```verdict\n"
                f'{{"verdict": "{reviewer_verdict[0]}", "findings": []}}\n```'
            )
        return f"{phase} output"

    monkeypatch.setattr(orchestrator, "_run_phase", fake_phase)

    result = orchestrator.run_orchestrator(
        repo_path=repo,
        task="Update the value with regression coverage",
        config_path=config,
        branch_mode="in_place",
        base_branch="main",
        agent_branch="agent/cumulative-tests",
    )

    assert result.status == expected_status
    assert _git(repo, "branch", "--show-current") == "agent/cumulative-tests"
    assert _git(repo, "rev-parse", "main") == main_revision
    assert _git(repo, "rev-list", "--count", "main..agent/cumulative-tests") == expected_commits
    if verdict == "approve":
        assert _git(repo, "status", "--short") == ""
        assert _git(repo, "log", "-1", "--pretty=%s").startswith("agent-loop:")
    else:
        assert _git(repo, "status", "--short")
    assert "checkpoint commit" in result.report_path.read_text(encoding="utf-8")

    if verdict == "revise":
        reviewer_verdict[0] = "approve"
        resumed = orchestrator.run_orchestrator(
            repo_path=repo,
            task="Update the value with regression coverage",
            config_path=config,
            branch_mode="in_place",
            base_branch="main",
            agent_branch="agent/cumulative-tests",
            resume_from_run_dir=result.run_dir,
        )
        assert resumed.status == "completed"
        assert _git(repo, "status", "--short") == ""
        assert _git(repo, "rev-list", "--count", "main..agent/cumulative-tests") == "1"
