"""Deterministic hook-style guardrails for commands and agent phases.

Two layers:

* Built-in hooks (``pre_command_hook``, ``post_command_hook``, ``pre_phase_hook``,
  ``post_phase_hook``) are fixed Python functions: the blocked-command gate and
  the metadata that feeds ``phase_events.jsonl``. They always run.
* Custom hooks are operator-configured external commands declared under a
  ``hooks:`` mapping in the run configuration (which may live target-local, the
  same trust model as ``verification.commands``). They run without a shell,
  receive context through ``AGENT_LOOP_*`` environment variables, and are
  validated against the blocked-command list at load time. ``pre_*`` events are
  gates: a non-zero exit aborts the phase/command. ``post_*`` events are
  informational: a non-zero exit is logged as a warning and never masks the
  actual result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os
import shlex
import shutil
import subprocess
import sys

from agent.log import get_logger
from agent.policies import is_command_blocked

logger = get_logger()

HOOK_EVENTS = ("pre_phase", "post_phase", "pre_command", "post_command")
_GATING_EVENTS = frozenset({"pre_phase", "pre_command"})
DEFAULT_HOOK_TIMEOUT_SECONDS = 60


class HookViolationError(RuntimeError):
    """Raised when a hook rejects an unsafe action."""


def split_command(command: str) -> list[str]:
    """Split a command string into argv using platform-appropriate rules.

    POSIX splitting eats backslashes, which corrupts Windows paths such as
    ``C:\\tools\\python``; ``posix=False`` preserves them on Windows.
    """
    return shlex.split(command, posix=sys.platform != "win32")


def resolve_executable(arguments: list[str]) -> list[str]:
    """Resolve argv[0] against PATH so shim scripts launch without a shell.

    On Windows, ``CreateProcess`` only appends ``.exe`` when searching, so npm
    (``npm.cmd``) and similar launcher scripts are not found by bare name; a
    ``shutil.which`` lookup honors ``PATHEXT`` and returns the full script path.
    """
    resolved = shutil.which(arguments[0])
    if resolved is None:
        return arguments
    return [resolved, *arguments[1:]]


@dataclass(frozen=True)
class HookCommand:
    """One configured hook command with its execution limit."""

    command: str
    timeout_seconds: int = DEFAULT_HOOK_TIMEOUT_SECONDS


@dataclass(frozen=True)
class HooksConfig:
    """Custom hook commands grouped by event."""

    hooks: dict[str, list[HookCommand]] = field(default_factory=dict)

    def for_event(self, event: str) -> list[HookCommand]:
        return self.hooks.get(event, [])

    @property
    def is_empty(self) -> bool:
        return not any(self.hooks.values())


def load_hooks_config(config: dict[str, Any], blocked_commands: list[str]) -> HooksConfig:
    """Parse and validate the optional ``hooks:`` mapping of a run configuration.

    Each event maps to a list of entries; an entry is either a command string or
    a ``{command, timeout_seconds}`` mapping. Hook commands obey the same
    blocked-command policy as verification commands, so a hook cannot smuggle in
    what the block list forbids.
    """
    raw = config.get("hooks")
    if raw is None:
        return HooksConfig()
    if not isinstance(raw, dict):
        raise ValueError("Configuration 'hooks' must be a YAML mapping of event -> command list")
    hooks: dict[str, list[HookCommand]] = {}
    for event, entries in raw.items():
        if event not in HOOK_EVENTS:
            raise ValueError(
                f"Unknown hook event '{event}'; valid events: {', '.join(HOOK_EVENTS)}"
            )
        if not isinstance(entries, list):
            raise ValueError(f"Hook event '{event}' must map to a list of commands")
        parsed: list[HookCommand] = []
        for entry in entries:
            if isinstance(entry, str):
                command, timeout_seconds = entry, DEFAULT_HOOK_TIMEOUT_SECONDS
            elif isinstance(entry, dict):
                unknown_keys = sorted(set(entry) - {"command", "timeout_seconds"})
                if unknown_keys:
                    raise ValueError(
                        f"Hook entry for '{event}' has unknown key(s): {', '.join(unknown_keys)}"
                    )
                command = entry.get("command")
                timeout_seconds = entry.get("timeout_seconds", DEFAULT_HOOK_TIMEOUT_SECONDS)
            else:
                raise ValueError(
                    f"Hook entries for '{event}' must be strings or {{command, timeout_seconds}}"
                )
            if not isinstance(command, str) or not command.strip():
                raise ValueError(f"Hook entry for '{event}' requires a non-empty 'command'")
            if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
                raise ValueError(
                    f"Hook entry for '{event}' requires a positive integer 'timeout_seconds'"
                )
            if is_command_blocked(command, blocked_commands):
                raise ValueError(f"Hook command for '{event}' is blocked by policy: {command}")
            parsed.append(HookCommand(command=command.strip(), timeout_seconds=timeout_seconds))
        hooks[event] = parsed
    return HooksConfig(hooks=hooks)


def run_custom_hooks(
    event: str,
    hooks_config: HooksConfig | None,
    repo_path: Path,
    context: dict[str, str] | None = None,
) -> None:
    """Run every configured hook for *event* with ``AGENT_LOOP_*`` context.

    Gating events (``pre_phase``, ``pre_command``) raise
    :class:`HookViolationError` on a non-zero exit, aborting the guarded action.
    Informational events log a warning and continue, so a broken reporting hook
    can never change a run's outcome. A hook that cannot start counts as a
    failure of that hook, following the same gating rule.
    """
    if hooks_config is None:
        return
    entries = hooks_config.for_event(event)
    if not entries:
        return
    environment = {
        **os.environ,
        "AGENT_LOOP_EVENT": event,
        "AGENT_LOOP_REPO": str(repo_path),
        **{f"AGENT_LOOP_{key.upper()}": str(value) for key, value in (context or {}).items()},
    }
    for entry in entries:
        failure: str | None = None
        try:
            completed = subprocess.run(
                resolve_executable(split_command(entry.command)),
                cwd=repo_path,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=entry.timeout_seconds,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            failure = f"could not run: {error}"
        else:
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
                failure = f"exit code {completed.returncode}: {detail}"
        if failure is None:
            logger.debug("Hook %s ok: %s", event, entry.command)
        elif event in _GATING_EVENTS:
            raise HookViolationError(f"Hook '{event}' rejected the action ({entry.command}): {failure}")
        else:
            logger.warning("Hook %s failed (non-blocking): %s (%s)", event, entry.command, failure)


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
