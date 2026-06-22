"""Controlled execution of configured verification commands."""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess


def run_verification_commands(repo_path: Path, commands: list[str]) -> dict[str, str]:
    """Run supplied verification commands without invoking a shell.

    Callers should pass only commands explicitly listed in their configuration.
    Each output includes the process exit code, stdout, and stderr.
    """
    results: dict[str, str] = {}
    for command in commands:
        try:
            arguments = shlex.split(command)
            if not arguments:
                results[command] = "Skipped empty command."
                continue
            completed = subprocess.run(
                arguments,
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            results[command] = (
                f"exit code: {completed.returncode}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            results[command] = f"Could not run command: {error}"
    return results
