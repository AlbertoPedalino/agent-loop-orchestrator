"""Deterministic hook-style guardrails for commands and agent phases."""

from __future__ import annotations

from pathlib import Path

from agent.policies import is_command_blocked


class HookViolationError(RuntimeError):
    """Raised when a hook rejects an unsafe action."""


def pre_command_hook(command: str, blocked_commands: list[str]) -> None:
    """Reject commands matching configured blocked substrings."""
    if is_command_blocked(command, blocked_commands):
        raise HookViolationError(f"Blocked command: {command}")


def post_command_hook(command: str, return_code: int, output: str) -> dict[str, str | int]:
    """Return deterministic metadata suitable for a run log."""
    return {
        "command": command,
        "return_code": return_code,
        "output_length": len(output),
        "status": "passed" if return_code == 0 else "failed",
    }


def pre_phase_hook(phase: str, repo_path: Path, task: str) -> dict[str, str]:
    """Return phase metadata before dispatching an agent provider."""
    return {
        "phase": phase,
        "repo_path": str(repo_path.expanduser().resolve()),
        "task_length": str(len(task)),
    }


def post_phase_hook(phase: str, output: str) -> dict[str, str]:
    """Return phase metadata after an agent provider responds."""
    return {
        "phase": phase,
        "output_length": str(len(output)),
        "status": "completed",
    }
