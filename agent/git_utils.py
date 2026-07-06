"""Safe Git inspection and local worktree helpers."""

from __future__ import annotations

from dataclasses import dataclass
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
        encoding="utf-8",
        errors="replace",
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


def get_git_status(path: Path, timeout_seconds: int = 30) -> str:
    """Return concise Git working-tree status or a timeout-safe diagnostic."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    try:
        result = _run_git(path, "status", "--short", timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return f"Git status timed out after {timeout_seconds} seconds."
    except OSError as error:
        return f"Unable to read Git status: {error}"
    return result.stdout if result.returncode == 0 else result.stderr.strip() or "Unable to read Git status."


def get_git_diff(path: Path, timeout_seconds: int = 30) -> str:
    """Return an unstaged Git diff with timeout-safe diagnostics."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    try:
        result = _run_git(path, "diff", "--no-ext-diff", timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return f"Git diff timed out after {timeout_seconds} seconds."
    except OSError as error:
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
    """Return whether a fetched remote-tracking branch exists locally.

    Call :func:`fetch_remote` explicitly when a fresh network query is required.
    Keeping this check local makes worktree setup reliable in restricted or
    offline environments and avoids unexpected remote calls.
    """
    result = _run_git(
        path, "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"
    )
    return result.returncode == 0


def fetch_remote(path: Path, remote: str = "origin") -> str:
    """Fetch a named remote without changing branches or pushing."""
    result = _run_git(path, "fetch", remote, timeout=180)
    return _git_output_or_error(result, f"fetch from '{remote}'")


def find_worktree_for_branch(repo_path: Path, branch_name: str) -> Path | None:
    """Return an existing linked worktree checked out at *branch_name*, if any."""
    result = _run_git(repo_path, "worktree", "list", "--porcelain")
    output = _git_output_or_error(result, "worktree listing")
    current_worktree: Path | None = None
    expected_ref = f"refs/heads/{branch_name}"
    for line in output.splitlines() + [""]:
        if line.startswith("worktree "):
            current_worktree = Path(line.removeprefix("worktree "))
        elif line == "":
            current_worktree = None
        elif line == f"branch {expected_ref}" and current_worktree is not None:
            return current_worktree.resolve()
    return None


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


def remove_worktree(repo_path: Path, worktree_path: Path) -> str:
    """Remove a registered linked worktree without deleting its branch.

    Removal is intentionally non-forced: Git refuses dirty worktrees, which
    keeps uncommitted agent output from being deleted by cleanup policy.
    """
    resolved_repo_path = repo_path.expanduser().resolve()
    resolved_worktree_path = worktree_path.expanduser().resolve()
    if not ensure_git_repo(resolved_repo_path):
        raise GitOperationError(f"Not a Git repository: {resolved_repo_path}")
    if resolved_worktree_path == resolved_repo_path:
        raise ValueError("Refusing to remove the source repository as a worktree")

    result = _run_git(
        resolved_repo_path,
        "worktree",
        "remove",
        str(resolved_worktree_path),
        timeout=180,
    )
    return _git_output_or_error(result, "worktree removal")


@dataclass(frozen=True)
class InPlaceBranchResult:
    """Outcome of checking out an agent branch inside the target repository."""

    final_branch: str
    created: bool
    reused: bool


def checkout_branch(path: Path, branch_name: str) -> str:
    """Check out an existing local branch in place."""
    result = _run_git(path, "checkout", branch_name, timeout=120)
    return _git_output_or_error(result, f"checkout '{branch_name}'")


def create_and_checkout_branch(path: Path, branch_name: str, base_ref: str) -> str:
    """Create a new branch from *base_ref* and check it out in place."""
    result = _run_git(path, "checkout", "-b", branch_name, base_ref, timeout=120)
    return _git_output_or_error(result, f"branch creation '{branch_name}'")


def checkout_in_place_agent_branch(
    repo_path: Path,
    agent_branch: str,
    base_branch: str,
    remote: str = "origin",
    allow_dirty: bool = False,
) -> InPlaceBranchResult:
    """Create or reuse an agent branch directly in the target repository.

    Unlike :func:`create_worktree`, this switches branches in the same working
    directory. It never pushes, commits, merges, or touches a protected branch.
    It refuses to switch away from a dirty working tree unless *allow_dirty* is
    set, so uncommitted work is never silently abandoned.
    """
    resolved_repo_path = repo_path.expanduser().resolve()
    if not ensure_git_repo(resolved_repo_path):
        raise GitOperationError(f"Not a Git repository: {resolved_repo_path}")
    if not agent_branch.strip():
        raise ValueError("Agent branch name must not be empty")
    if is_protected_branch(agent_branch):
        raise ValueError(f"Refusing to use protected agent branch: {agent_branch}")
    if not base_branch or not base_branch.strip():
        raise ValueError("A base branch is required for in-place branch mode")
    if agent_branch.strip() == base_branch.strip():
        raise ValueError("Agent branch must differ from the base branch")

    if get_git_status(resolved_repo_path).strip() and not allow_dirty:
        raise GitOperationError(
            "Working tree is dirty; refusing to switch branches. Commit or stash "
            "changes, or allow a dirty repository explicitly."
        )

    if branch_exists(resolved_repo_path, agent_branch):
        checkout_branch(resolved_repo_path, agent_branch)
        return InPlaceBranchResult(agent_branch, created=False, reused=True)

    base_local = branch_exists(resolved_repo_path, base_branch)
    base_remote = remote_branch_exists(resolved_repo_path, remote, base_branch)
    if not base_local and not base_remote:
        fetch_remote(resolved_repo_path, remote)
        base_local = branch_exists(resolved_repo_path, base_branch)
        base_remote = remote_branch_exists(resolved_repo_path, remote, base_branch)

    if base_local:
        base_ref = base_branch
    elif base_remote:
        base_ref = f"{remote}/{base_branch}"
    else:
        raise GitOperationError(
            f"Base branch '{base_branch}' does not exist locally or on remote '{remote}'"
        )

    create_and_checkout_branch(resolved_repo_path, agent_branch, base_ref)
    return InPlaceBranchResult(agent_branch, created=True, reused=False)
