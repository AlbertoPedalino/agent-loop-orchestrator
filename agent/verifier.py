"""Policy-aware execution of configured verification commands."""

from __future__ import annotations

from pathlib import Path
import subprocess

from agent.hooks import (
    HooksConfig,
    post_command_hook,
    pre_command_hook,
    resolve_executable,
    run_custom_hooks,
    split_command,
)
from agent.policies import validate_commands_allowed

_split_command = split_command
_resolve_executable = resolve_executable


def run_verification_commands(
    repo_path: Path,
    commands: list[str],
    blocked_commands: list[str] | None = None,
    timeout_seconds: int | None = None,
    hooks_config: HooksConfig | None = None,
) -> dict[str, str]:
    """Run all allowed verification commands without invoking a shell.

    Results are collected for every command, including non-zero exits, instead
    of stopping at the first test failure. Custom ``pre_command`` hooks gate
    each command (a rejection is recorded as that command's result);
    ``post_command`` hooks receive the outcome and never affect it.
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
            run_custom_hooks(
                "pre_command", hooks_config, resolved_repo_path, {"command": command}
            )
        except Exception as error:  # noqa: BLE001 - a hook rejection is this command's result
            results[command] = f"exit code: rejected\nstdout:\n\nstderr:\n{error}"
            continue
        try:
            arguments = _split_command(command)
            if not arguments:
                results[command] = "exit code: skipped\nstdout:\n\nstderr:\nEmpty command."
                continue
            completed = subprocess.run(
                _resolve_executable(arguments),
                cwd=resolved_repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
            results[command] = (
                f"exit code: {completed.returncode}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
            post_command_hook(command, completed.returncode, completed.stdout + completed.stderr)
            run_custom_hooks(
                "post_command",
                hooks_config,
                resolved_repo_path,
                {"command": command, "return_code": str(completed.returncode)},
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            results[command] = f"exit code: unavailable\nstdout:\n\nstderr:\nCould not run command: {error}"
    return results


def verification_passed(results: dict[str, str]) -> bool:
    """Return whether every collected verification result exited successfully.

    An empty mapping (no verification commands configured) passes vacuously:
    "nothing to verify" is treated as success. Configure at least one command
    when a run should fail unless a real check passes.
    """
    return all(result.startswith("exit code: 0\n") for result in results.values())
