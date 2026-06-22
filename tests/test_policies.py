"""Tests for command-blocking policy checks."""

import pytest

from agent.policies import PolicyError, is_command_blocked, validate_commands_allowed


BLOCKED_COMMANDS = [
    "git push",
    "git commit",
    "rm -rf",
    "wandb sweep",
    "wandb agent",
    "jupyter nbconvert --execute",
    "docker compose down -v",
]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push origin main", True),
        ("GIT COMMIT -m 'change'", True),
        ("rm -rf build", True),
        ("wandb sweep sweep.yaml", True),
        ("wandb agent team/project/sweep", True),
        ("jupyter nbconvert --execute notebook.ipynb", True),
        ("docker compose down -v", True),
        ("pytest -q tests/test_policies.py", False),
        ("git status --short", False),
    ],
)
def test_command_blocking(command: str, expected: bool) -> None:
    assert is_command_blocked(command, BLOCKED_COMMANDS) is expected


def test_empty_blocked_command_is_ignored() -> None:
    assert not is_command_blocked("pytest -q", [""])


def test_validate_commands_allowed_rejects_any_blocked_command() -> None:
    with pytest.raises(PolicyError, match="git commit"):
        validate_commands_allowed(["pytest -q", "git commit -m unsafe"], BLOCKED_COMMANDS)
