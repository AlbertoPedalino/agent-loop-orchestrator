"""Policy-aware execution of configured verification commands."""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess

from agent.hooks import post_command_hook, pre_command_hook
from agent.policies import validate_commands_allowed


def run_verification_commands(
    repo_path: Path,
    commands: list[str],
    blocked_commands: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, str]:
    """Run all allowed verification commands without invoking a shell.

    Results are collected for every command, including non-zero exits, instead
    of stopping at the first test failure.
    """
    resolved_repo_path = repo_path.expanduser().resolve()
    if not resolved_repo_path.is_dir():
        raise NotADirectoryError(f"Verification repository path is not a directory: {resolved_repo_path}")
    validate_commands_allowed(commands, blocked_commands or [])

    timeout = timeout_seconds if timeout_seconds is not None else 120
    if timeout <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    results: dict[str, str] = {}
    for command in commands:
        pre_command_hook(command, blocked_commands or [])
        try:
            arguments = shlex.split(command, posix=True)
            if not arguments:
                results[command] = "exit code: skipped\nstdout:\n\nstderr:\nEmpty command."
                continue
            completed = subprocess.run(
                arguments,
                cwd=resolved_repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            results[command] = (
                f"exit code: {completed.returncode}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
            post_command_hook(command, completed.returncode, completed.stdout + completed.stderr)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            results[command] = f"exit code: unavailable\nstdout:\n\nstderr:\nCould not run command: {error}"
    return results


def verification_passed(results: dict[str, str]) -> bool:
    """Return whether every collected verification result exited successfully."""
    return all(result.startswith("exit code: 0\n") for result in results.values())
