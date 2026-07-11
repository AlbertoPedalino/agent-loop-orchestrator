"""Tests for read-only Git helper behavior."""

from pathlib import Path
import subprocess

import pytest

from agent import git_utils
from agent.git_utils import ensure_git_repo


def test_non_repo_path_is_not_a_git_repo(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repository"
    non_repo.mkdir()

    assert not ensure_git_repo(non_repo)


def test_git_status_timeout_returns_clear_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        git_utils,
        "_run_git",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["git", "status"], 7)
        ),
    )

    assert git_utils.get_git_status(tmp_path, timeout_seconds=7) == "Git status timed out after 7 seconds."


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def test_git_diff_includes_staged_and_untracked_files(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)

    tracked.write_text("base\nstaged-change\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    (tmp_path / "brand-new.txt").write_text("new-content\n", encoding="utf-8")

    diff = git_utils.get_git_diff(tmp_path)

    # Staged edits and never-added files must both be visible to fixer/reviewer.
    assert "+staged-change" in diff
    assert "brand-new.txt" in diff
    assert "+new-content" in diff


def test_changed_and_deleted_paths_are_reported(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "old-test.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)

    (tmp_path / "old-test.py").unlink()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")

    assert set(git_utils.get_changed_paths(tmp_path)) == {
        "old-test.py",
        "tests/test_new.py",
    }
    assert git_utils.get_deleted_paths(tmp_path) == ["old-test.py"]


def test_checkpoint_refuses_unexpected_or_protected_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    (tmp_path / "base.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(git_utils.GitOperationError, match="unexpected branch"):
        git_utils.commit_all_changes(tmp_path, "checkpoint", "agent/expected")

    current = git_utils.get_current_branch(tmp_path)
    if current != "main":
        subprocess.run(["git", "-C", str(tmp_path), "checkout", "-qb", "main"], check=True)
    with pytest.raises(git_utils.GitOperationError, match="protected branch"):
        git_utils.commit_all_changes(tmp_path, "checkpoint", "main")
