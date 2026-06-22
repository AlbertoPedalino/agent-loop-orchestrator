"""Tests for read-only Git helper behavior."""

from pathlib import Path

from agent.git_utils import ensure_git_repo


def test_non_repo_path_is_not_a_git_repo(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repository"
    non_repo.mkdir()

    assert not ensure_git_repo(non_repo)
