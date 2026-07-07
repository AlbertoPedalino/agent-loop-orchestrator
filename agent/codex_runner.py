"""Safe subprocess adapter for the Codex CLI."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
import json
import subprocess
import tempfile
import threading

from agent.api_keys import subprocess_env
from agent.log import get_logger
from agent.permissions import is_read_only_tool_set


class CodexRunnerError(RuntimeError):
    """Raised when Codex cannot complete a requested phase."""


class CodexTransientError(CodexRunnerError):
    """A phase failure that looks retryable (e.g. a flaky OS sandbox spawn)."""


DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_TRANSIENT_RETRIES = 1

# Windows-heavy substrings seen when Codex's own sandboxed shell fails to launch a
# child process. These are environment hiccups, not prompt or code errors, so a
# fresh attempt usually succeeds. Matched case-insensitively against Codex output.
_TRANSIENT_FAILURE_SIGNATURES = (
    "sandbox",
    "failed to spawn",
    "failed to launch",
    "the process cannot access",
    "resource temporarily unavailable",
    "insufficient system resources",
    "os error 1455",
)


def _looks_transient(text: str) -> bool:
    lowered = text.lower()
    return any(signature in lowered for signature in _TRANSIENT_FAILURE_SIGNATURES)


def _validate_repo_path(repo_path: Path) -> Path:
    resolved_path = repo_path.expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Codex repository path does not exist: {resolved_path}")
    if not resolved_path.is_dir():
        raise NotADirectoryError(f"Codex repository path is not a directory: {resolved_path}")
    return resolved_path


def _sandbox_for_tools(allowed_tools: list[str] | None) -> str:
    """Map the phase's mutability to a Codex sandbox mode."""
    return "read-only" if is_read_only_tool_set(allowed_tools or []) else "workspace-write"


def _build_codex_command(
    *,
    repo_path: Path,
    output_path: Path,
    sandbox_mode: str,
    stream: bool,
    cli_prefix: list[str] | None = None,
) -> list[str]:
    """Build a Codex exec command without putting the prompt on argv."""
    command = list(cli_prefix or ["codex"])
    command.extend(
        [
            "--ask-for-approval",
            "never",
            "exec",
            "--cd",
            str(repo_path),
            "--sandbox",
            sandbox_mode,
            "--color",
            "never",
            "--output-last-message",
            str(output_path),
        ]
    )
    if stream:
        command.append("--json")
    command.append("-")
    return command


def _powershell_prefix_for_script(script_path: Path) -> list[str]:
    powershell = (
        shutil.which("pwsh")
        or shutil.which("pwsh.exe")
        or shutil.which("powershell")
        or shutil.which("powershell.exe")
    )
    if powershell is None:
        raise CodexRunnerError(
            f"Codex CLI was found as PowerShell script {script_path}, "
            "but no PowerShell executable was found."
        )
    return [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]


def _prefix_for_codex_path(path: str) -> list[str]:
    resolved = Path(path)
    if os.name == "nt":
        if resolved.suffix.lower() == ".ps1":
            return _powershell_prefix_for_script(resolved)
        if resolved.suffix.lower() in {".cmd", ".bat"}:
            powershell_shim = resolved.with_suffix(".ps1")
            if powershell_shim.is_file():
                return _powershell_prefix_for_script(powershell_shim)
    return [str(resolved)]


def _resolve_codex_command_prefix() -> list[str]:
    """Resolve npm/PowerShell shims that Python cannot execute as plain ``codex``."""
    if os.name == "nt":
        for executable_name in ("codex.ps1", "codex.cmd", "codex.exe", "codex.bat"):
            found = shutil.which(executable_name)
            if found is not None:
                return _prefix_for_codex_path(found)
    found = shutil.which("codex")
    if found is not None:
        return _prefix_for_codex_path(found)
    return ["codex"]


def _safe_command_display(command: list[str]) -> str:
    """Return a diagnostic command display. The prompt is passed via stdin."""
    return " ".join(command)


def _phase_label(phase: str | None) -> str:
    return phase or "codex"


def _augment_prompt(
    prompt: str,
    *,
    allowed_tools: list[str] | None,
    disallowed_tools: list[str] | None,
    permission_mode: str | None,
    sandbox_mode: str,
) -> str:
    """Add a provider-specific policy note for constraints Codex has no CLI flag for."""
    lines = [
        prompt.rstrip(),
        "",
        "## Codex Execution Policy",
        "",
        f"- Sandbox mode: {sandbox_mode}.",
        "- Approval policy: never ask for interactive approval; choose a safe alternative instead.",
    ]
    if allowed_tools:
        lines.append(f"- Phase tool policy from the orchestrator: {', '.join(allowed_tools)}.")
    if disallowed_tools:
        lines.append(
            "- Do not run shell commands matching these deny rules: "
            + ", ".join(disallowed_tools)
            + "."
        )
    if permission_mode is not None:
        lines.append(f"- Claude permission mode equivalent requested: {permission_mode}.")
    if sandbox_mode == "read-only":
        lines.append("- This phase is analysis-only: do not modify files.")
    return "\n".join(lines)


def _read_last_message(output_path: Path, stdout: str) -> str:
    if output_path.is_file():
        try:
            output = output_path.read_text(encoding="utf-8")
        except OSError as error:
            raise CodexRunnerError(
                f"Could not read Codex output file {output_path}: {error}"
            ) from error
        if output.strip():
            return output
    if stdout.strip():
        return stdout
    raise CodexRunnerError("Codex returned no result text.")


def _event_text(event: dict[str, Any]) -> str:
    for key in ("message", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    item = event.get("item")
    if isinstance(item, dict):
        for key in ("text", "message"):
            value = item.get(key)
            if isinstance(value, str):
                return value
    return ""


def _handle_stream_line(line: str, phase: str | None, stdout_lines: list[str]) -> None:
    stripped = line.strip()
    if not stripped:
        return
    stdout_lines.append(line)
    logger = get_logger()
    label = _phase_label(phase)
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("[%s] %s", label, stripped[:300])
        return
    if not isinstance(event, dict):
        return
    event_type = str(event.get("type") or event.get("event") or "event")
    text = _event_text(event).strip()
    if text:
        logger.info("[%s] %s: %s", label, event_type, " ".join(text.split())[:200])
    else:
        logger.debug("[%s] %s", label, event_type)


def _run_streaming(
    command: list[str],
    prompt: str,
    cwd: Path,
    output_path: Path,
    timeout_seconds: int,
    phase: str | None,
    env: dict[str, str] | None = None,
) -> str:
    safe_command = _safe_command_display(command)
    prompt_path = output_path.with_name("prompt.md")
    try:
        prompt_path.write_text(prompt, encoding="utf-8")
    except OSError as error:
        raise CodexRunnerError(
            f"Codex phase '{_phase_label(phase)}' could not write prompt file: {error}"
        ) from error

    try:
        prompt_file = prompt_path.open("r", encoding="utf-8")
    except OSError as error:
        raise CodexRunnerError(
            f"Codex phase '{_phase_label(phase)}' could not read prompt file: {error}"
        ) from error

    try:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=prompt_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as error:
            raise CodexRunnerError(
                f"Codex phase '{_phase_label(phase)}' could not start: {error}\n"
                f"Command: {safe_command}"
            ) from error
    finally:
        prompt_file.close()

    timed_out = threading.Event()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            process.kill()
        except OSError:
            pass

    def _read_stderr() -> None:
        if process.stderr is None:
            return
        try:
            stderr_lines.extend(process.stderr.readlines())
        except OSError as error:
            stderr_lines.append(f"<stderr read failed: {error}>")

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    watchdog = threading.Timer(timeout_seconds, _kill_on_timeout)
    stderr_thread.start()
    watchdog.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            _handle_stream_line(line, phase, stdout_lines)
        return_code = process.wait()
        stderr_thread.join(timeout=1)
    finally:
        watchdog.cancel()

    stderr = "".join(stderr_lines)
    if timed_out.is_set():
        raise CodexRunnerError(
            f"Codex phase '{_phase_label(phase)}' timed out after {timeout_seconds} seconds.\n"
            f"Command: {safe_command}"
        )
    if return_code != 0:
        combined_output = "".join(stdout_lines) + stderr
        error_type = CodexTransientError if _looks_transient(combined_output) else CodexRunnerError
        raise error_type(
            f"Codex phase '{_phase_label(phase)}' failed with return code {return_code}.\n"
            f"Command: {safe_command}\nstdout:\n{''.join(stdout_lines)}\nstderr:\n{stderr}"
        )
    return _read_last_message(output_path, "".join(stdout_lines))


def _run_captured(
    command: list[str],
    prompt: str,
    cwd: Path,
    output_path: Path,
    timeout_seconds: int,
    phase: str | None,
    env: dict[str, str] | None = None,
) -> str:
    safe_command = _safe_command_display(command)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodexRunnerError(
            f"Codex phase '{_phase_label(phase)}' could not start: {error}\n"
            f"Command: {safe_command}"
        ) from error

    if result.returncode != 0:
        combined_output = (result.stdout or "") + (result.stderr or "")
        error_type = CodexTransientError if _looks_transient(combined_output) else CodexRunnerError
        raise error_type(
            f"Codex phase '{_phase_label(phase)}' failed with return code {result.returncode}.\n"
            f"Command: {safe_command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return _read_last_message(output_path, result.stdout)


def run_codex_prompt(
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
    transient_retries: int = DEFAULT_TRANSIENT_RETRIES,
    backend: str = "cli",
) -> str:
    """Run ``codex exec`` in *repo_path* and return its final message.

    A transient failure (a flaky OS sandbox spawn, not a prompt or code error) is
    retried up to ``transient_retries`` times before propagating.

    *backend* selects how the CLI authenticates: ``cli`` (subscription login;
    API-key variables are stripped from the subprocess environment) or ``api``
    (an ``OPENAI_API_KEY``/``CODEX_API_KEY`` resolved from the environment or
    the orchestrator ``.env`` is injected, so the run bills the API account).
    """
    if max_budget_usd is not None:
        raise ValueError("max_budget_usd is supported by Claude CLI, not by Codex CLI")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if transient_retries < 0:
        raise ValueError("transient_retries must be zero or greater")

    resolved_repo_path = _validate_repo_path(repo_path)
    sandbox_mode = _sandbox_for_tools(allowed_tools)
    codex_prompt = _augment_prompt(
        prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode=permission_mode,
        sandbox_mode=sandbox_mode,
    )
    with tempfile.TemporaryDirectory(prefix="agent-loop-codex-") as temp_dir:
        output_path = Path(temp_dir) / "last-message.md"
        command = _build_codex_command(
            repo_path=resolved_repo_path,
            output_path=output_path,
            sandbox_mode=sandbox_mode,
            stream=stream,
            cli_prefix=_resolve_codex_command_prefix(),
        )
        env = subprocess_env("codex", backend)
        runner = _run_streaming if stream else _run_captured
        for attempt in range(transient_retries + 1):
            try:
                return runner(
                    command,
                    codex_prompt,
                    resolved_repo_path,
                    output_path,
                    timeout_seconds,
                    phase,
                    env,
                )
            except CodexTransientError:
                if attempt >= transient_retries:
                    raise
                get_logger().warning(
                    "Codex phase '%s' hit a transient failure; retrying (%d/%d).",
                    _phase_label(phase),
                    attempt + 1,
                    transient_retries,
                )
    raise AssertionError("unreachable: codex retry loop exited without returning")
