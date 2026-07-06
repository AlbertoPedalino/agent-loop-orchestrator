"""Safe subprocess adapter for the Claude Code CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import subprocess
import threading

from agent.log import get_logger, summarize_tool_use


class ClaudeRunnerError(RuntimeError):
    """Raised when Claude Code cannot complete a requested phase."""


DEFAULT_TIMEOUT_SECONDS = 600


def _validate_repo_path(repo_path: Path) -> Path:
    resolved_path = repo_path.expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Claude repository path does not exist: {resolved_path}")
    if not resolved_path.is_dir():
        raise NotADirectoryError(f"Claude repository path is not a directory: {resolved_path}")
    return resolved_path


def _build_claude_command(
    *,
    allowed_tools: list[str] | None,
    disallowed_tools: list[str] | None,
    permission_mode: str | None,
    output_format: str | None,
    max_budget_usd: float | None,
) -> list[str]:
    """Build a Claude print-mode command. The prompt is passed via stdin.

    Keeping the prompt off ``argv`` avoids the Windows command-line length limit
    (``WinError 206``) for large prompts (e.g. an implementer prompt carrying the
    full planner output and project memory). Tool lists are passed as a single
    comma-separated value (the CLI accepts a comma- or space-separated list). The
    agent's tool policy is enforced here so read-only phases cannot edit and
    blocked commands are denied; this is no longer left to prompt convention.
    ``stream-json`` output additionally requires ``--verbose`` in print mode, so
    it is added automatically.
    """
    command = ["claude", "-p"]
    if allowed_tools:
        command.extend(["--allowedTools", ",".join(allowed_tools)])
    if disallowed_tools:
        command.extend(["--disallowedTools", ",".join(disallowed_tools)])
    if permission_mode is not None:
        command.extend(["--permission-mode", permission_mode])
    if output_format is not None:
        command.extend(["--output-format", output_format])
        if output_format == "stream-json":
            command.append("--verbose")
    if max_budget_usd is not None:
        command.extend(["--max-budget-usd", str(max_budget_usd)])
    return command


def _safe_command_display(command: list[str]) -> str:
    """Return a diagnostic command display. The prompt is passed via stdin."""
    return " ".join(command)


def _phase_label(phase: str | None) -> str:
    return phase or "claude"


def _handle_stream_line(line: str, phase: str | None, state: dict[str, Any]) -> None:
    """Parse one stream-json line and emit a human-readable log line.

    Final result text is recorded in ``state['result']``; assistant text is also
    accumulated in ``state['text']`` as a fallback when no result event arrives.
    """
    stripped = line.strip()
    if not stripped:
        return
    logger = get_logger()
    label = _phase_label(phase)
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("[%s] unparsed stream line: %s", label, stripped[:200])
        return
    if not isinstance(event, dict):
        return

    event_type = event.get("type")
    if event_type == "system" and event.get("subtype") == "init":
        logger.info("[%s] ▶ session start (model=%s)", label, event.get("model", "?"))
    elif event_type == "assistant":
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    state.setdefault("text", []).append(text)
                    logger.info("[%s] │ %s", label, text)
            elif block.get("type") == "tool_use":
                logger.info(
                    "[%s] 🔧 %s",
                    label,
                    summarize_tool_use(block.get("name", "tool"), block.get("input", {})),
                )
    elif event_type == "user":
        logger.debug("[%s] ◂ tool result", label)
    elif event_type == "result":
        state["result_seen"] = True
        state["result_subtype"] = event.get("subtype")
        if isinstance(event.get("result"), str):
            state["result"] = event["result"]
        logger.info(
            "[%s] ✔ done (turns=%s, cost=$%s)",
            label,
            event.get("num_turns", "?"),
            event.get("total_cost_usd", "?"),
        )


def _run_streaming(
    command: list[str], prompt: str, cwd: Path, timeout_seconds: int, phase: str | None
) -> str:
    """Run Claude with stream-json output, logging events live, return result text."""
    safe_command = _safe_command_display(command)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        raise ClaudeRunnerError(
            f"Claude phase '{_phase_label(phase)}' could not start: {error}\n"
            f"Command: {safe_command}"
        ) from error

    def _feed_stdin() -> None:
        # Feed the prompt from a thread so a prompt larger than the pipe buffer
        # cannot deadlock against the stdout reader below.
        try:
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()
        except OSError:
            # The process exited before consuming the prompt; the non-zero
            # return code is reported below.
            pass

    stdin_writer = threading.Thread(target=_feed_stdin, daemon=True)
    stdin_writer.start()

    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        process.kill()

    watchdog = threading.Timer(timeout_seconds, _kill_on_timeout)
    watchdog.start()
    state: dict[str, Any] = {}
    try:
        assert process.stdout is not None
        for line in process.stdout:
            _handle_stream_line(line, phase, state)
        stderr = process.stderr.read() if process.stderr else ""
        return_code = process.wait()
    finally:
        watchdog.cancel()
        stdin_writer.join(timeout=5)

    if timed_out.is_set():
        raise ClaudeRunnerError(
            f"Claude phase '{_phase_label(phase)}' timed out after {timeout_seconds} seconds.\n"
            f"Command: {safe_command}"
        )

    result = state.get("result") or "\n".join(state.get("text", []))
    if return_code != 0:
        # A `result` event means Claude completed the turn and reported a stop
        # reason (e.g. budget or turn limit). The produced output is still valid,
        # so keep it instead of discarding the whole phase; only a non-zero exit
        # with no result is a hard failure.
        if state.get("result_seen") and result:
            get_logger().warning(
                "[%s] exited %d (%s) but produced a result; keeping partial output.",
                _phase_label(phase),
                return_code,
                state.get("result_subtype") or "limit",
            )
            return result
        raise ClaudeRunnerError(
            f"Claude phase '{_phase_label(phase)}' failed with return code {return_code}.\n"
            f"Command: {safe_command}\nstderr:\n{stderr}"
        )
    if not result:
        raise ClaudeRunnerError(
            f"Claude phase '{_phase_label(phase)}' returned no result text.\nCommand: {safe_command}"
        )
    return result


def _run_captured(
    command: list[str], prompt: str, cwd: Path, timeout_seconds: int, phase: str | None
) -> str:
    """Run Claude with buffered output (no live logging) and return stdout."""
    safe_command = _safe_command_display(command)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ClaudeRunnerError(
            f"Claude phase '{_phase_label(phase)}' could not start: {error}\n"
            f"Command: {safe_command}"
        ) from error

    if result.returncode != 0:
        raise ClaudeRunnerError(
            f"Claude phase '{_phase_label(phase)}' failed with return code {result.returncode}.\n"
            f"Command: {safe_command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout


def run_claude_prompt(
    prompt: str,
    repo_path: Path,
    *,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    permission_mode: str | None = None,
    max_budget_usd: float | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    stream: bool = True,
    phase: str | None = None,
) -> str:
    """Run ``claude -p`` in *repo_path* and return its result text.

    The command is always executed as an argument list with ``shell=False`` and
    the prompt is delivered on stdin (never on ``argv``), so large prompts do not
    hit the OS command-line length limit. Tool permissions are passed through so
    the backend enforces the phase's allow/deny policy. When *stream* is true (the
    default), ``stream-json`` output is parsed live and each tool call and
    assistant message is logged to the terminal as it happens; otherwise output is
    buffered until the phase ends.
    """
    if max_budget_usd is not None and max_budget_usd <= 0:
        raise ValueError("max_budget_usd must be greater than zero when provided")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    resolved_repo_path = _validate_repo_path(repo_path)
    command = _build_claude_command(
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode=permission_mode,
        output_format="stream-json" if stream else "text",
        max_budget_usd=max_budget_usd,
    )
    runner = _run_streaming if stream else _run_captured
    return runner(command, prompt, resolved_repo_path, timeout_seconds, phase)
