"""Policy checks used to keep future orchestration runs bounded."""

from __future__ import annotations


def is_command_blocked(command: str, blocked_commands: list[str]) -> bool:
    """Return whether a configured blocked-command substring appears in *command*.

    Matching is case-insensitive so equivalent shell commands are handled
    consistently. Empty blocked entries are ignored.
    """
    normalized_command = command.lower()
    return any(
        blocked_command.strip().lower() in normalized_command
        for blocked_command in blocked_commands
        if blocked_command.strip()
    )
