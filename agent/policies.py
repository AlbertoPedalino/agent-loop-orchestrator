"""Policy checks used to keep orchestration runs bounded."""

from __future__ import annotations


class PolicyError(RuntimeError):
    """Raised when configured policy forbids a requested command."""


def is_command_blocked(command: str, blocked_commands: list[str]) -> bool:
    """Return whether a blocked-command substring appears in *command*.

    Matching is case-insensitive. Empty blocked entries are ignored.
    """
    normalized_command = command.casefold()
    return any(
        blocked_command.strip().casefold() in normalized_command
        for blocked_command in blocked_commands
        if blocked_command.strip()
    )


def validate_commands_allowed(commands: list[str], blocked_commands: list[str]) -> None:
    """Raise when any configured command violates the block list."""
    for command in commands:
        if is_command_blocked(command, blocked_commands):
            raise PolicyError(f"Blocked verification command: {command}")
