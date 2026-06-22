"""Tests for protected branch and worktree command safety."""

from pathlib import Path
import subprocess

import pytest

from agent import git_utils


@pytest.mark.parametrize("branch", ["main", "MASTER", "develop", "production", "release/1.0"])
def test_protected_branches(branch: str) -> None:
    assert git_utils.is_protected_branch(branch)


def test_safe_branch_is_not_protected() -> None:
    assert not git_utils.is_protected_branch("agent/fix-tests")


def test_create_worktree_rejects_protected_agent_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(git_utils, "ensure_git_repo", lambda path: True)
    with pytest.raises(ValueError, match="protected"):
        git_utils.create_worktree(tmp_path, tmp_path / "worktrees", "main", "base")


def test_create_worktree_uses_git_c_and_safe_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(git_utils, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(git_utils, "branch_exists", lambda path, branch: branch == "base")
    monkeypatch.setattr(git_utils, "remote_branch_exists", lambda path, remote, branch: False)

    def fake_run(path: Path, *arguments: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")

    monkeypatch.setattr(git_utils, "_run_git", fake_run)
    worktree = git_utils.create_worktree(repo, tmp_path / "worktrees", "agent/test", "base")

    assert worktree == (tmp_path / "worktrees" / "agent-test").resolve()
    assert commands == [("worktree", "add", "-b", "agent/test", str(worktree), "base")]
