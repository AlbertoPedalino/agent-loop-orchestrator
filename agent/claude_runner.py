"""Boundary for a future Claude Code or Claude Agent SDK integration."""

from __future__ import annotations

from pathlib import Path


def run_claude_prompt(
    prompt: str, repo_path: Path, max_turns: int | None = None
) -> str:
    """Run a prompt through Claude Code in a future implementation.

    This will eventually execute a carefully controlled command equivalent to
    ``claude -p "<prompt>"`` in ``repo_path``, or call the Claude Agent SDK.
    """
    del prompt, repo_path, max_turns
    raise NotImplementedError("Claude Code execution is not implemented in the skeleton.")
