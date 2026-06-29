"""Optional, lazy Claude Agent SDK adapter."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path
from typing import Any

from agent.log import get_logger


class AgentSdkUnavailableError(RuntimeError):
    """Raised when the optional Claude Agent SDK extra is not installed."""


class AgentSdkRunnerError(RuntimeError):
    """Raised when the installed SDK does not support the adapter boundary."""


def _validate_repo_path(repo_path: Path) -> Path:
    resolved_path = repo_path.expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Agent SDK repository path does not exist: {resolved_path}")
    if not resolved_path.is_dir():
        raise NotADirectoryError(f"Agent SDK repository path is not a directory: {resolved_path}")
    return resolved_path


def _message_text(message: Any) -> str:
    """Extract textual content from common SDK message shapes."""
    if isinstance(message, str):
        return message
    for attribute in ("result", "text", "content"):
        value = getattr(message, attribute, None)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [
                item if isinstance(item, str) else str(getattr(item, "text", ""))
                for item in value
            ]
            return "".join(part for part in parts if part)
    return ""


async def _collect_sdk_response(response: Any, phase: str | None = None) -> str:
    if inspect.isawaitable(response):
        response = await response
    if hasattr(response, "__aiter__"):
        logger = get_logger()
        label = phase or "claude"
        parts: list[str] = []
        async for message in response:
            text = _message_text(message)
            if text:
                parts.append(text)
                logger.info("[%s] │ %s", label, " ".join(text.split())[:200])
        return "\n".join(parts)
    return _message_text(response)


async def run_agent_sdk_prompt(
    prompt: str,
    repo_path: Path,
    *,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    max_turns: int | None = None,
    permission_mode: str | None = None,
    timeout_seconds: int | None = None,
    phase: str | None = None,
) -> str:
    """Run a prompt through the optional SDK using a small compatibility adapter.

    The SDK is imported lazily so normal CLI installations do not require it.
    The same allow/deny tool policy and permission mode used by the CLI backend
    are forwarded here, so phase enforcement is identical across backends. The
    public SDK surface changes independently of this project; unsupported
    versions fail at this adapter boundary instead of silently guessing APIs.
    """
    resolved_repo_path = _validate_repo_path(repo_path)
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero when provided")
    try:
        sdk = importlib.import_module("claude_agent_sdk")
    except ImportError as error:
        raise AgentSdkUnavailableError(
            "Claude Agent SDK is not installed. Install with: pip install '.[sdk]'"
        ) from error

    query = getattr(sdk, "query", None)
    options_class = getattr(sdk, "ClaudeAgentOptions", None)
    if not callable(query) or options_class is None:
        raise AgentSdkRunnerError(
            "Installed claude_agent_sdk does not expose query and ClaudeAgentOptions. "
            "Update the SDK adapter for this version."
        )

    options_kwargs: dict[str, Any] = {"cwd": str(resolved_repo_path)}
    if allowed_tools:
        options_kwargs["allowed_tools"] = allowed_tools
    if disallowed_tools:
        options_kwargs["disallowed_tools"] = disallowed_tools
    if max_turns is not None:
        options_kwargs["max_turns"] = max_turns
    if permission_mode is not None:
        options_kwargs["permission_mode"] = permission_mode

    try:
        options = options_class(**options_kwargs)
        response = query(prompt=prompt, options=options)
        if timeout_seconds is not None:
            output = await asyncio.wait_for(
                _collect_sdk_response(response, phase), timeout_seconds
            )
        else:
            output = await _collect_sdk_response(response, phase)
    except asyncio.TimeoutError as error:
        phase_name = phase or "unknown"
        raise AgentSdkRunnerError(
            f"Agent SDK phase '{phase_name}' timed out after {timeout_seconds} seconds."
        ) from error
    except Exception as error:  # SDK exceptions are version-specific.
        phase_name = phase or "unknown"
        raise AgentSdkRunnerError(f"Agent SDK phase '{phase_name}' failed: {error}") from error

    if not output:
        raise AgentSdkRunnerError(f"Agent SDK phase '{phase or 'unknown'}' returned no text output.")
    return output


def run_agent_sdk_prompt_sync(*args: Any, **kwargs: Any) -> str:
    """Synchronous wrapper for the orchestrator's non-async control flow."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_agent_sdk_prompt(*args, **kwargs))
    raise AgentSdkRunnerError(
        "run_agent_sdk_prompt_sync cannot be used inside an active event loop; "
        "await run_agent_sdk_prompt instead."
    )
