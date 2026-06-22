"""Safe Git inspection and local worktree helpers."""

from __future__ import annotations

from pathlib import Path
import subprocess


PROTECTED_BRANCHES = {"main", "master", "develop", "production"}


class GitOperationError(RuntimeError):
    """Raised when a safe Git operation cannot be completed."""


def _git_command(path: Path, *arguments: str) -> list[str]:
    return ["git", "-C", str(path.expanduser().resolve()), *arguments]


def _run_git(
    path: Path, *arguments: str, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    """Run Git using ``git -C`` and no shell."""
    return subprocess.run(
        _git_command(path, *arguments),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _git_output_or_error(result: subprocess.CompletedProcess[str], operation: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no Git output"
        raise GitOperationError(f"Git {operation} failed: {detail}")
    return result.stdout


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
    """Return concise Git working-tree status or a diagnostic."""
    try:
        result = _run_git(path, "status", "--short")
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"Unable to read Git status: {error}"
    return result.stdout if result.returncode == 0 else result.stderr.strip() or "Unable to read Git status."


def get_git_diff(path: Path) -> str:
    """Return an unstaged Git diff without executing external diff tools."""
    try:
        result = _run_git(path, "diff", "--no-ext-diff")
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"Unable to read Git diff: {error}"
    return result.stdout if result.returncode == 0 else result.stderr.strip() or "Unable to read Git diff."


def is_protected_branch(branch: str) -> bool:
    """Return whether a branch must never be used as an agent branch."""
    normalized = branch.strip().casefold()
    return normalized in PROTECTED_BRANCHES or normalized.startswith("release/")


def get_current_branch(path: Path) -> str:
    """Return the checked-out branch name, rejecting detached HEAD state."""
    result = _run_git(path, "rev-parse", "--abbrev-ref", "HEAD")
    branch = _git_output_or_error(result, "current-branch lookup").strip()
    if branch == "HEAD":
        raise GitOperationError("Repository is in detached HEAD state")
    return branch


def branch_exists(path: Path, branch: str) -> bool:
    """Return whether a local branch exists."""
    result = _run_git(path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    return result.returncode == 0


def remote_branch_exists(path: Path, remote: str, branch: str) -> bool:
    """Return whether a remote-tracking or advertised remote branch exists."""
    local_tracking = _run_git(
        path, "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"
    )
    if local_tracking.returncode == 0:
        return True
    result = _run_git(path, "ls-remote", "--exit-code", "--heads", remote, branch, timeout=120)
    return result.returncode == 0


def fetch_remote(path: Path, remote: str = "origin") -> str:
    """Fetch a named remote without changing branches or pushing."""
    result = _run_git(path, "fetch", remote, timeout=180)
    return _git_output_or_error(result, f"fetch from '{remote}'")


def create_worktree(
    repo_path: Path,
    worktree_root: Path,
    branch_name: str,
    base_branch: str,
    remote: str = "origin",
) -> Path:
    """Create a new local agent branch and linked worktree from a safe base.

    This function never pushes, deletes branches, removes worktrees, or checks
    out a protected agent branch in the source repository.
    """
    resolved_repo_path = repo_path.expanduser().resolve()
    if not ensure_git_repo(resolved_repo_path):
        raise GitOperationError(f"Not a Git repository: {resolved_repo_path}")
    if not branch_name.strip():
        raise ValueError("Agent branch name must not be empty")
    if is_protected_branch(branch_name):
        raise ValueError(f"Refusing to create protected agent branch: {branch_name}")
    if branch_exists(resolved_repo_path, branch_name) or remote_branch_exists(
        resolved_repo_path, remote, branch_name
    ):
        raise GitOperationError(f"Agent branch already exists: {branch_name}")

    if branch_exists(resolved_repo_path, base_branch):
        base_ref = base_branch
    elif remote_branch_exists(resolved_repo_path, remote, base_branch):
        base_ref = f"{remote}/{base_branch}"
    else:
        raise GitOperationError(
            f"Base branch '{base_branch}' does not exist locally or on remote '{remote}'"
        )

    resolved_worktree_root = worktree_root.expanduser().resolve()
    worktree_path = resolved_worktree_root / branch_name.replace("/", "-")
    if worktree_path.exists():
        raise FileExistsError(f"Worktree path already exists: {worktree_path}")
    resolved_worktree_root.mkdir(parents=True, exist_ok=True)

    result = _run_git(
        resolved_repo_path,
        "worktree",
        "add",
        "-b",
        branch_name,
        str(worktree_path),
        base_ref,
        timeout=180,
    )
    _git_output_or_error(result, "worktree creation")
    return worktree_path
