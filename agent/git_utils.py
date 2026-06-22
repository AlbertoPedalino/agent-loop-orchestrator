"""Small, read-only Git helpers for inspecting a target repository."""

from __future__ import annotations

from pathlib import Path
import subprocess


def _run_git(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Git command without a shell."""
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def ensure_git_repo(path: Path) -> bool:
    """Return whether *path* is inside a Git work tree."""
    if not path.is_dir():
        return False
    try:
        result = _run_git(path, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def get_git_status(path: Path) -> str:
    """Return the concise Git working-tree status or an error message."""
    result = _run_git(path, "status", "--short")
    if result.returncode == 0:
        return result.stdout
    return result.stderr.strip() or "Unable to read Git status."


def get_git_diff(path: Path) -> str:
    """Return the unstaged Git diff or an error message."""
    result = _run_git(path, "diff", "--no-ext-diff")
    if result.returncode == 0:
        return result.stdout
    return result.stderr.strip() or "Unable to read Git diff."
