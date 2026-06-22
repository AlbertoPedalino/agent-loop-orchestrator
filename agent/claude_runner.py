"""Safe subprocess adapter for the Claude Code CLI."""

from __future__ import annotations

from pathlib import Path
import subprocess


class ClaudeRunnerError(RuntimeError):
    """Raised when Claude Code cannot complete a requested phase."""


def _validate_repo_path(repo_path: Path) -> Path:
    resolved_path = repo_path.expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Claude repository path does not exist: {resolved_path}")
    if not resolved_path.is_dir():
        raise NotADirectoryError(f"Claude repository path is not a directory: {resolved_path}")
    return resolved_path


def _build_claude_command(
    prompt: str,
    *,
    max_turns: int | None,
    output_format: str | None,
    max_budget_usd: float | None,
) -> list[str]:
    """Build a Claude command without exposing prompt text to a shell."""
    command = ["claude", "-p", prompt]
    if max_turns is not None:
        command.extend(["--max-turns", str(max_turns)])
    if output_format is not None:
        command.extend(["--output-format", output_format])
    if max_budget_usd is not None:
        command.extend(["--max-budget-usd", str(max_budget_usd)])
    return command


def _safe_command_display(command: list[str]) -> str:
    """Return a diagnostic command display without leaking the prompt."""
    displayed = command.copy()
    prompt_index = displayed.index("-p") + 1
    displayed[prompt_index] = "<redacted-prompt>"
    return " ".join(displayed)


def run_claude_prompt(
    prompt: str,
    repo_path: Path,
    max_turns: int | None = None,
    output_format: str = "text",
    max_budget_usd: float | None = None,
    phase: str | None = None,
) -> str:
    """Run ``claude -p`` in *repo_path* and return its standard output.

    The command is always executed as an argument list with ``shell=False``.
    Future Agent SDK support lives in :mod:`agent.sdk_runner`; this function is
    deliberately only the Claude Code CLI boundary.
    """
    if max_turns is not None and max_turns <= 0:
        raise ValueError("max_turns must be greater than zero when provided")
    if max_budget_usd is not None and max_budget_usd <= 0:
        raise ValueError("max_budget_usd must be greater than zero when provided")

    resolved_repo_path = _validate_repo_path(repo_path)
    command = _build_claude_command(
        prompt,
        max_turns=max_turns,
        output_format=output_format,
        max_budget_usd=max_budget_usd,
    )
    safe_command = _safe_command_display(command)

    try:
        result = subprocess.run(
            command,
            cwd=resolved_repo_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        phase_name = phase or "unknown"
        raise ClaudeRunnerError(
            f"Claude phase '{phase_name}' could not start: {error}\nCommand: {safe_command}"
        ) from error

    if result.returncode != 0:
        phase_name = phase or "unknown"
        raise ClaudeRunnerError(
            f"Claude phase '{phase_name}' failed with return code {result.returncode}.\n"
            f"Command: {safe_command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout
