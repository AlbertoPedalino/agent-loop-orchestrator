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
