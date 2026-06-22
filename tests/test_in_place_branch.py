"""Tests for in-place agent branch creation and branch-mode resolution."""

from pathlib import Path
import subprocess

import pytest

from agent import git_utils
from agent import orchestrator
from agent.git_utils import InPlaceBranchResult


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yaml"


# --------------------------------------------------------------------------- #
# git_utils.checkout_in_place_agent_branch
# --------------------------------------------------------------------------- #


def _patch_clean_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_utils, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(git_utils, "get_git_status", lambda path, **kwargs: "")


def test_checkout_in_place_creates_branch_from_local_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_clean_repo(monkeypatch)
    monkeypatch.setattr(git_utils, "branch_exists", lambda path, branch: branch == "base")
    monkeypatch.setattr(git_utils, "remote_branch_exists", lambda path, remote, branch: False)
    commands: list[tuple[str, ...]] = []

    def fake_run(path: Path, *arguments: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")

    monkeypatch.setattr(git_utils, "_run_git", fake_run)

    result = git_utils.checkout_in_place_agent_branch(tmp_path, "agent/x", "base")

    assert result == InPlaceBranchResult("agent/x", created=True, reused=False)
    assert commands == [("checkout", "-b", "agent/x", "base")]


def test_checkout_in_place_reuses_existing_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_clean_repo(monkeypatch)
    monkeypatch.setattr(git_utils, "branch_exists", lambda path, branch: True)
    commands: list[tuple[str, ...]] = []

    def fake_run(path: Path, *arguments: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")

    monkeypatch.setattr(git_utils, "_run_git", fake_run)

    result = git_utils.checkout_in_place_agent_branch(tmp_path, "agent/x", "base")

    assert result == InPlaceBranchResult("agent/x", created=False, reused=True)
    assert commands == [("checkout", "agent/x")]


def test_checkout_in_place_uses_remote_base_when_local_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_clean_repo(monkeypatch)
    monkeypatch.setattr(git_utils, "branch_exists", lambda path, branch: False)
    monkeypatch.setattr(git_utils, "remote_branch_exists", lambda path, remote, branch: branch == "base")
    commands: list[tuple[str, ...]] = []

    def fake_run(path: Path, *arguments: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")

    monkeypatch.setattr(git_utils, "_run_git", fake_run)

    result = git_utils.checkout_in_place_agent_branch(tmp_path, "agent/x", "base", remote="origin")

    assert result.created is True
    assert commands == [("checkout", "-b", "agent/x", "origin/base")]


def test_checkout_in_place_rejects_protected_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(git_utils, "ensure_git_repo", lambda path: True)
    with pytest.raises(ValueError, match="protected"):
        git_utils.checkout_in_place_agent_branch(tmp_path, "main", "base")


def test_checkout_in_place_rejects_agent_equal_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(git_utils, "ensure_git_repo", lambda path: True)
    with pytest.raises(ValueError, match="differ from the base"):
        git_utils.checkout_in_place_agent_branch(tmp_path, "agent/x", "agent/x")


def test_checkout_in_place_blocks_dirty_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(git_utils, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(git_utils, "get_git_status", lambda path, **kwargs: " M file.py")
    with pytest.raises(git_utils.GitOperationError, match="dirty"):
        git_utils.checkout_in_place_agent_branch(tmp_path, "agent/x", "base")


def test_checkout_in_place_allows_dirty_when_explicitly_permitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(git_utils, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(git_utils, "get_git_status", lambda path, **kwargs: " M file.py")
    monkeypatch.setattr(git_utils, "branch_exists", lambda path, branch: branch == "base")
    monkeypatch.setattr(git_utils, "remote_branch_exists", lambda path, remote, branch: False)
    monkeypatch.setattr(
        git_utils,
        "_run_git",
        lambda *args, **kwargs: subprocess.CompletedProcess(["git"], 0, stdout="", stderr=""),
    )

    result = git_utils.checkout_in_place_agent_branch(
        tmp_path, "agent/x", "base", allow_dirty=True
    )

    assert result.created is True


# --------------------------------------------------------------------------- #
# branch-mode resolution helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("branch_mode", "use_worktree", "expected"),
    [
        (None, False, ("none", False)),
        (None, True, ("worktree", True)),
        ("worktree", False, ("worktree", True)),
        ("in_place", False, ("in_place", False)),
        ("none", False, ("none", False)),
        ("in_place", True, ("worktree", True)),  # use_worktree wins
    ],
)
def test_resolve_branch_mode(branch_mode, use_worktree, expected) -> None:
    assert orchestrator._resolve_branch_mode(branch_mode, use_worktree) == expected


@pytest.mark.parametrize(
    ("create_branch", "plan_only", "setup_only", "expected"),
    [
        ("auto", True, False, False),  # plan-only never branches
        ("auto", False, False, True),  # full loop branches
        ("auto", False, True, False),  # setup-only auto does not branch
        ("always", False, True, True),  # explicit request branches even for setup
        ("always", True, False, False),  # plan-only still wins
        ("never", False, False, False),
    ],
)
def test_wants_in_place_branch(create_branch, plan_only, setup_only, expected) -> None:
    assert orchestrator._wants_in_place_branch(create_branch, plan_only, setup_only) is expected


# --------------------------------------------------------------------------- #
# orchestrator integration
# --------------------------------------------------------------------------- #


def _patch_orchestrator_common(
    monkeypatch: pytest.MonkeyPatch, run_dir: Path, branch: str = "agent/x"
) -> None:
    run_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: branch)
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path, **kwargs: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path, **kwargs: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)


def test_plan_only_in_place_does_not_create_or_checkout_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_orchestrator_common(monkeypatch, tmp_path / "run", branch="agent/plan")
    monkeypatch.setattr(
        orchestrator,
        "checkout_in_place_agent_branch",
        lambda *args, **kwargs: pytest.fail("plan-only must not touch branches"),
    )
    monkeypatch.setattr(orchestrator, "run_claude_prompt", lambda *args, **kwargs: "safe plan")

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="inspect",
        config_path=DEFAULT_CONFIG,
        branch_mode="in_place",
        plan_only=True,
    )

    assert result.status == "plan-only-complete"
    report = result.report_path.read_text(encoding="utf-8")
    assert "branch mode**: in_place" in report
    assert "branch created**: no" in report


def test_full_loop_in_place_creates_agent_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_orchestrator_common(monkeypatch, tmp_path / "run")
    calls: list[tuple] = []

    def fake_checkout(repo_path, agent_branch, base_branch, **kwargs):
        calls.append((agent_branch, base_branch, kwargs.get("allow_dirty")))
        return InPlaceBranchResult(agent_branch, created=True, reused=False)

    monkeypatch.setattr(orchestrator, "checkout_in_place_agent_branch", fake_checkout)
    monkeypatch.setattr(orchestrator, "_run_phase", lambda *, phase, **kwargs: f"{phase} output")
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: {"pytest -q": "exit code: 0\nstdout:\n\nstderr:\n"},
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="implement",
        config_path=DEFAULT_CONFIG,
        branch_mode="in_place",
        base_branch="base",
        agent_branch="agent/x",
        allow_dirty=True,
    )

    assert result.status == "completed"
    assert result.target_repo_path == tmp_path.resolve()
    assert result.worktree_path is None
    assert calls == [("agent/x", "base", True)]
    report = result.report_path.read_text(encoding="utf-8")
    assert "branch created**: yes" in report
    assert "branch reused**: no" in report


def test_full_loop_in_place_reuses_existing_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_orchestrator_common(monkeypatch, tmp_path / "run")
    monkeypatch.setattr(
        orchestrator,
        "checkout_in_place_agent_branch",
        lambda *args, **kwargs: InPlaceBranchResult("agent/x", created=False, reused=True),
    )
    monkeypatch.setattr(orchestrator, "_run_phase", lambda *, phase, **kwargs: f"{phase} output")
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: {"pytest -q": "exit code: 0\nstdout:\n\nstderr:\n"},
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="implement",
        config_path=DEFAULT_CONFIG,
        branch_mode="in_place",
        base_branch="base",
        agent_branch="agent/x",
    )

    assert result.status == "completed"
    report = result.report_path.read_text(encoding="utf-8")
    assert "branch created**: no" in report
    assert "branch reused**: yes" in report


def test_full_loop_in_place_requires_agent_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "feature/base")
    with pytest.raises(ValueError, match="agent branch is required"):
        orchestrator.run_orchestrator(
            repo_path=tmp_path,
            task="implement",
            config_path=DEFAULT_CONFIG,
            branch_mode="in_place",
            base_branch="base",
        )


def test_full_loop_in_place_rejects_agent_equal_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "feature/base")
    with pytest.raises(ValueError, match="differ from the base"):
        orchestrator.run_orchestrator(
            repo_path=tmp_path,
            task="implement",
            config_path=DEFAULT_CONFIG,
            branch_mode="in_place",
            base_branch="agent/x",
            agent_branch="agent/x",
        )


def test_full_loop_in_place_rejects_protected_agent_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "feature/base")
    with pytest.raises(ValueError, match="protected agent branch"):
        orchestrator.run_orchestrator(
            repo_path=tmp_path,
            task="implement",
            config_path=DEFAULT_CONFIG,
            branch_mode="in_place",
            base_branch="base",
            agent_branch="develop",
        )


def test_full_loop_in_place_blocks_dirty_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_orchestrator_common(monkeypatch, tmp_path / "run", branch="feature/base")
    # Use the real checkout helper so the dirty guard runs; make its repo dirty.
    monkeypatch.setattr(git_utils, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(git_utils, "get_git_status", lambda path, **kwargs: " M file.py")

    with pytest.raises(git_utils.GitOperationError, match="dirty"):
        orchestrator.run_orchestrator(
            repo_path=tmp_path,
            task="implement",
            config_path=DEFAULT_CONFIG,
            branch_mode="in_place",
            base_branch="base",
            agent_branch="agent/x",
        )


def test_use_worktree_preserves_worktree_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    worktree = tmp_path / "existing-worktree"
    run_dir.mkdir()
    worktree.mkdir()
    _patch_orchestrator_common(monkeypatch, run_dir, branch="agent/plan")
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda *args: worktree)
    monkeypatch.setattr(
        orchestrator,
        "checkout_in_place_agent_branch",
        lambda *args, **kwargs: pytest.fail("worktree mode must not branch in place"),
    )
    monkeypatch.setattr(orchestrator, "_run_phase", lambda *, phase, **kwargs: f"{phase} output")
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: {"pytest -q": "exit code: 0\nstdout:\n\nstderr:\n"},
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="full run",
        config_path=DEFAULT_CONFIG,
        use_worktree=True,
        base_branch="base",
        agent_branch="agent/plan",
    )

    assert result.status == "completed"
    assert result.target_repo_path == worktree
    report = result.report_path.read_text(encoding="utf-8")
    assert "branch mode**: worktree" in report


def test_report_records_branch_mode_and_creation_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_orchestrator_common(monkeypatch, tmp_path / "run")
    monkeypatch.setattr(
        orchestrator,
        "checkout_in_place_agent_branch",
        lambda *args, **kwargs: InPlaceBranchResult("agent/x", created=True, reused=False),
    )
    monkeypatch.setattr(orchestrator, "_run_phase", lambda *, phase, **kwargs: f"{phase} output")
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: {"pytest -q": "exit code: 0\nstdout:\n\nstderr:\n"},
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="implement",
        config_path=DEFAULT_CONFIG,
        branch_mode="in_place",
        create_branch="auto",
        base_branch="base",
        agent_branch="agent/x",
    )

    report = result.report_path.read_text(encoding="utf-8")
    for fragment in (
        "original repo path**:",
        "resolved repo path**:",
        "branch mode**: in_place",
        "create branch mode**: auto",
        "original branch**:",
        "base branch**: base",
        "agent branch**: agent/x",
        "branch created**: yes",
        "branch reused**: no",
        "final branch**:",
        "working tree dirty at end**:",
    ):
        assert fragment in report, fragment
